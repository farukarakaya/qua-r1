# SPDX-License-Identifier: Apache-2.0

import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch

from trading_r1.assembly import build_run_registry, builtin_skill_registry, catalog_tool_registry
from trading_r1.runtime.capabilities import capabilities_catalog
from trading_r1.runtime.dispatch import dispatch
from trading_r1.runtime.evidence import EvidenceStore, lineage
from trading_r1.runtime.modes import MODES
from trading_r1.runtime.prompts import render_capabilities
from trading_r1.runtime.session import recorder
from trading_r1.runtime.session.store import SessionStore
from trading_r1.runtime.skill_registry import SkillSessionState
from trading_r1.runtime.taxonomy import classify_issues
from trading_r1.runtime.tool_registry import ToolSessionState
from trading_r1.tools.core.discovery import NAME
from trading_r1.tools.data.market import bist_screen
from trading_r1.tools.data.market.bist_screen import screen_capture_scope


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.skills = builtin_skill_registry()
        self.bind(EvidenceStore(), "run1")

    def bind(self, store, run):
        self.state = ToolSessionState(evidence=store, run=run)
        self.registry = build_run_registry(
            catalog_tool_registry(), self.skills, self.state, SkillSessionState(), lambda *_: None
        )

    async def call(self, name, args):
        return await dispatch(name, args, registry=self.registry, session_state=self.state)

    def source(self, tool, payload, **provenance):
        # Frozen inputs shaped like existing filing, news and quote results.
        return self.state.evidence.add(
            tool=tool, args={}, payload=payload, meta=provenance, run=self.state.run
        ).id

    @staticmethod
    def decision(symbol, evidence_id, path, **fields):
        return {
            "symbol": symbol,
            "track": "supply_chain",
            "verdict": "watch",
            "change": "First production line qualified; prior lines were pilot only.",
            "business_impact": "Capacity financing is committed despite negative earnings.",
            "counterevidence": "There is no binding volume commitment yet.",
            "next_milestone": "Check sales and collections in the next filing.",
            "evidence": [{"evidence_id": evidence_id, "path": path}],
            **fields,
        }

    async def test_bist_pages_and_verdicts_survive_resume_without_counting_unseen_names(self):
        rules = self.skills.get("discovery")
        loaded = await self.call("load_skill", {"skills": ["discovery"]})
        self.assertTrue(loaded.ok, loaded.text)
        self.assertEqual(rules.domain, "equity")
        self.assertIn(NAME, loaded.payload["skills"][0]["tools_loaded"])

        rows = [
            {"symbol": "XIST:SUPPL", "name": "Supplier", "market_cap_usd": 80, "pe": -6},
            {"symbol": "XIST:PROMO", "name": "Peer", "market_cap_usd": 60, "pe": 10},
            {"symbol": "XIST:UNSEEN", "name": "Unseen", "market_cap_usd": 50},
            {"symbol": "XIST:LATER", "name": "Later", "market_cap_usd": 40},
        ]
        with (
            tempfile.TemporaryDirectory() as home,
            patch.dict(os.environ, {"TR1_HOME": home}, clear=True),
        ):
            sessions = SessionStore(home)
            rec = sessions.begin("istfin-fixture", "Discover supplier opportunities", {})
            detach = self.state.evidence.observe(rec.record_evidence)
            with (
                patch.object(bist_screen.bist, "screen_stocks", return_value=(rows, [])),
                screen_capture_scope(),
                recorder.bind(rec),
            ):
                page = await self.call(bist_screen.SPEC.name, {"limit": 2})
                self.assertTrue(page.ok, page.text)
                capture = page.payload["capture_id"]
                sup = self.source(
                    "data_market_istfin_disclosure",
                    {
                        "disclosure": {
                            "published_at": "2024-06-12T14:00:00Z",
                            "content": [
                                {"value": "Plant financing signed. Net earnings negative."}
                            ],
                        }
                    },
                )
                promo = self.source(
                    "data_market_bist",
                    {"rows": [{"text": "Nonbinding order worth about 0.2% of annual sales."}]},
                )
                watch = self.decision(
                    "SUPPL.IS",
                    sup,
                    "disclosure.content[0].value",
                    publication={"evidence_id": sup, "path": "disclosure.published_at"},
                )
                reject = self.decision(
                    "XIST:PROMO",
                    promo,
                    "rows[0].text",
                    verdict="pass",
                    change="Announced a nonbinding order.",
                    business_impact="Only 0.2% of annual sales even if delivered.",
                    counterevidence="Order is nonbinding and immaterial.",
                )
                unseen = await self.call(
                    NAME, {"capture_id": capture, "decisions": [watch | {"symbol": "XIST:UNSEEN"}]}
                )
                self.assertFalse(unseen.ok)
                self.assertIn("was not returned", unseen.text)
                saved = await self.call(NAME, {"capture_id": capture, "decisions": [watch, reject]})
                self.assertTrue(saved.ok, saved.text)
                self.assertIn(f"[{saved.evidence_id}]", saved.text)
                counts = (
                    "matched",
                    "returned",
                    "unseen",
                    "examined",
                    "rejected",
                    "watch",
                    "eligible",
                )
                self.assertEqual(tuple(saved.payload[key] for key in counts), (4, 2, 2, 2, 1, 1, 0))
                self.assertEqual(
                    saved.payload["verdicts"], {"XIST:SUPPL": "watch", "XIST:PROMO": "pass"}
                )
                self.assertEqual(
                    saved.payload["decisions"][0]["published_at"], "2024-06-12T14:00:00Z"
                )
                self.assertIsNone(saved.payload["decisions"][0]["valuation"])
                pages = [page.evidence_id]
                for _ in range(2):
                    next_page = await self.call(
                        bist_screen.SPEC.name, {"cursor": page.payload["next_cursor"]}
                    )
                    self.assertTrue(next_page.ok, next_page.text)
                    pages.append(next_page.evidence_id)
            detach()
            rec.end(None, termination="answer")

            restored = EvidenceStore.from_list(SessionStore(home).evidence("istfin-fixture"))
            original = restored.get(saved.evidence_id)
            self.assertEqual(original.payload, saved.payload)
            self.assertTrue(original.recorded_at)
            self.assertEqual(
                set(lineage(saved.evidence_id, restored.all()).sources),
                {page.evidence_id, sup, promo},
            )
            other = EvidenceStore.from_list(SessionStore(home).evidence("another-session"))
            self.bind(other, "another-run")
            unavailable = await self.call(NAME, {"capture_id": capture})
            self.assertFalse(unavailable.ok)
            self.assertIn("No such BIST screen capture", unavailable.text)

            self.bind(restored, "run2")
            rec2 = sessions.begin("istfin-fixture", "Supplier update", {})
            detach = restored.observe(rec2.record_evidence)
            with recorder.bind(rec2):
                lost_order = self.source(
                    "data_market_bist",
                    {"rows": [{"text": "The customer cancelled qualification."}]},
                )
                revised = watch | {
                    "verdict": "pass",
                    "change": "The customer cancelled qualification.",
                    "business_impact": "New capacity has no confirmed demand.",
                    "counterevidence": "Customer cancellation invalidates the ramp thesis.",
                    "publication": None,
                    "evidence": [{"evidence_id": lost_order, "path": "rows[0].text"}],
                }
                updated = await self.call(NAME, {"capture_id": capture, "decisions": [revised]})
            detach()
            rec2.end(None, termination="answer")
            self.assertTrue(updated.ok, updated.text)
            self.assertEqual(tuple(updated.payload[key] for key in counts), (4, 4, 0, 2, 2, 0, 0))
            self.assertEqual(original.payload["verdicts"]["XIST:SUPPL"], "watch")
            reopened = SessionStore(home).evidence("istfin-fixture")
            self.assertEqual(reopened[-1]["payload"]["verdicts"]["XIST:SUPPL"], "pass")
            self.assertEqual(
                set(lineage(updated.evidence_id, EvidenceStore.from_list(reopened).all()).sources),
                {*pages, lost_order},
            )

    async def test_timing_requires_a_real_publication_field_and_valid_evidence(self):
        undated = self.source(
            "data_market_news",
            {
                "items": [
                    {
                        "title": "Plant expansion",
                        "published": "2024-06-12",
                        "time_basis": "discovered",
                    },
                    {
                        "title": "Unrelated item",
                        "published": "2024-06-12",
                        "time_basis": "published",
                    },
                ]
            },
        )
        past_filing = self.source(
            "data_equity_filings",
            {"filings": [{"form": "8-K", "report_date": "2024-06-01", "filed": "2024-06-12"}]},
        )
        future_filing = self.source(
            "data_equity_filings",
            {"filings": [{"form": "8-K", "report_date": "2024-06-01", "filed": "2999-06-12"}]},
        )
        quotes = self.source(
            "data_market_quotes",
            {"quotes": {"FIX": {"price": 30, "currency": "USD", "session_date": "2024-06-13"}}},
        )
        calc = await self.call(
            "calc",
            {"values": {"p": f"prev:{quotes}.quotes.FIX.price"}, "expression": "p * 10 / 15"},
        )
        self.assertTrue(calc.ok, calc.text)
        docs_hit = self.source(
            "search_docs", {"hits": [{"document_id": "d1", "summary": "Supplier note."}]}
        )
        watch = self.decision("FIX", undated, "items[0].title")
        hurdle = {
            "quote_evidence": quotes,
            "calculation": {"evidence_id": calc.evidence_id, "path": "result"},
            "assumptions": "Required earnings = price * 10m / 15x.",
            "plausible_range": "18 to 24 USDm.",
        }
        cases = [
            (watch | {"early_claim": True}, "needs a recorded publication date"),
            (
                watch | {"publication": {"evidence_id": undated, "path": "items[0].published"}},
                "not a publication time",
            ),
            (
                watch
                | {
                    "early_claim": True,
                    "publication": {"evidence_id": past_filing, "path": "filings[0].filed"},
                },
                "same evidence item cited in evidence",
            ),
            (
                watch
                | {
                    "verdict": "eligible",
                    "publication": {"evidence_id": past_filing, "path": "filings[0].filed"},
                    "price_hurdle": hurdle,
                },
                "same evidence item cited in evidence",
            ),
            (
                watch | {"publication": {"evidence_id": undated, "path": "items[1].published"}},
                "same evidence item cited in evidence",
            ),
            (
                self.decision(
                    "FIX",
                    past_filing,
                    "filings[0].filed",
                    publication={"evidence_id": past_filing, "path": "filings[0].filed"},
                ),
                "same evidence item cited in evidence",
            ),
            (
                watch
                | {"publication": {"evidence_id": future_filing, "path": "filings[0].report_date"}},
                "not a report or retrieval date",
            ),
            (
                self.decision(
                    "FIX",
                    future_filing,
                    "filings[0].form",
                    publication={"evidence_id": future_filing, "path": "filings[0].filed"},
                ),
                "in the future",
            ),
            (self.decision("FIX", "e999", "title"), "No discovery evidence e999"),
            (self.decision("FIX", docs_hit, "hits[0].summary"), "company or industry evidence"),
            (self.decision("FIX", undated, "items[9].title"), "missing or null value"),
            (
                watch | {"verdict": "eligible"},
                "Eligibility needs a publication date and price hurdle",
            ),
        ]
        for decision, message in cases:
            with self.subTest(message=message):
                out = await self.call(NAME, {"us_universe": ["FIX"], "decisions": [decision]})
                self.assertFalse(out.ok, out.text)
                self.assertIn(message, out.text)
        bad_us = await self.call(NAME, {"us_universe": ["VERYLONGNAME123.IS"]})
        self.assertFalse(bad_us.ok)
        self.assertIn("distinct list of plain US equity tickers", bad_us.text)
        coverage = await self.call(NAME, {"us_universe": ["FIX"]})
        self.assertTrue(coverage.ok, coverage.text)
        self.assertEqual(coverage.payload["examined"], 0)
        saved = await self.call(NAME, {"us_universe": ["FIX"], "decisions": [watch]})
        self.assertTrue(saved.ok, saved.text)
        self.assertIsNone(saved.payload["decisions"][0]["published_at"])
        self.assertEqual(saved.payload["watch"], 1)

    async def test_us_roster_batch_quote_and_reopened_lineage(self):
        filing = self.source(
            "data_equity_filings", {"filings": [{"form": "8-K", "filed": "2024-06-12"}]}
        )
        quotes = self.source(
            "data_market_quotes",
            {
                "quotes": {
                    "FIX": {"price": 30, "currency": "USD", "session_date": "2024-06-13"},
                    "PEER": {"price": 900, "currency": "USD", "session_date": "2024-06-13"},
                }
            },
        )
        peer_calc = await self.call(
            "calc",
            {"values": {"p": f"prev:{quotes}.quotes.PEER.price"}, "expression": "p * 10 / 15"},
        )
        mcap = await self.call(
            "calc",
            {"values": {"p": f"prev:{quotes}.quotes.FIX.price"}, "expression": "p * 10"},
        )
        calculated = await self.call(
            "calc",
            {"values": {"m": f"prev:{mcap.evidence_id}.result"}, "expression": "m / 15"},
        )
        inline = await self.call(
            "calc", {"expression": f"prev:{quotes}.quotes.FIX.price * 10 / 15"}
        )
        unlinked = await self.call("calc", {"expression": "30 * 10 / 15"})
        for result in (peer_calc, mcap, calculated, inline, unlinked):
            self.assertTrue(result.ok, result.text)
        decision = self.decision(
            "FIX",
            filing,
            "filings[0].form",
            verdict="eligible",
            publication={"evidence_id": filing, "path": "filings[0].filed"},
            price_hurdle={
                "quote_evidence": quotes,
                "calculation": {"evidence_id": calculated.evidence_id, "path": "result"},
                "assumptions": "Required annual USDm earnings = price times 10m shares / 15x.",
                "plausible_range": "18 to 24 USDm at expected plant utilization.",
            },
        )
        req = {"us_universe": ["PEER", "FIX"]}
        for bad_calc, msg in (
            (unlinked.evidence_id, "Chain the quote price for FIX"),
            (peer_calc.evidence_id, "Chain the quote price for FIX"),
        ):
            invalid = decision | {
                "price_hurdle": decision["price_hurdle"]
                | {"calculation": {"evidence_id": bad_calc, "path": "result"}}
            }
            failed = await self.call(NAME, req | {"decisions": [invalid]})
            self.assertFalse(failed.ok)
            self.assertIn(msg, failed.text)
        failed = await self.call(NAME, {"us_universe": ["PEER"], "decisions": [decision]})
        self.assertFalse(failed.ok)
        self.assertIn("on the supplied list", failed.text)
        with (
            tempfile.TemporaryDirectory() as home,
            patch.dict(os.environ, {"TR1_HOME": home}, clear=True),
        ):
            sessions = SessionStore(home)
            rec = sessions.begin("us-fixture", "Save US discovery", {})
            detach = self.state.evidence.observe(rec.record_evidence)
            for eid in (
                filing,
                quotes,
                mcap.evidence_id,
                calculated.evidence_id,
                inline.evidence_id,
            ):
                rec.record_evidence(self.state.evidence.get(eid))
            with recorder.bind(rec):
                saved = await self.call(NAME, req | {"decisions": [decision]})
            detach()
            rec.end(None, termination="answer")

            self.assertTrue(saved.ok, saved.text)
            self.assertEqual(saved.payload["us_universe"], ["FIX", "PEER"])
            self.assertEqual(saved.payload["matched"], 2)
            self.assertEqual(saved.payload["examined"], 1)
            self.assertEqual(saved.payload["eligible"], 1)
            self.assertEqual(
                saved.payload["decisions"][0]["valuation"],
                {
                    "price": 30,
                    "currency": "USD",
                    "quote_date": "2024-06-13",
                    "implied": 20,
                },
            )
            restored = EvidenceStore.from_list(SessionStore(home).evidence("us-fixture"))
            self.bind(restored, "reopened")
            walk = lineage(saved.evidence_id, restored.all())
            self.assertEqual(set(walk.sources), {filing, quotes})
            self.assertIn(mcap.evidence_id, {node.id for node in walk.nodes})
            self.assertIn(calculated.evidence_id, {node.id for node in walk.nodes})
            ev = await self.call("evidence", {"id": saved.evidence_id})
            self.assertTrue(ev.ok, ev.text)
            self.assertEqual(
                set(ev.payload["derived_from"]),
                {filing, quotes, calculated.evidence_id},
            )
            self.assertEqual(
                set(ev.payload["traces_back_to"]),
                {f"{filing} data_equity_filings", f"{quotes} data_market_quotes"},
            )
            inline_saved = await self.call(
                NAME,
                req
                | {
                    "decisions": [
                        decision
                        | {
                            "price_hurdle": decision["price_hurdle"]
                            | {"calculation": {"evidence_id": inline.evidence_id, "path": "result"}}
                        }
                    ]
                },
            )
            self.assertTrue(inline_saved.ok, inline_saved.text)
        reordered = await self.call(NAME, {"us_universe": ["fix", "PEER"]})
        self.assertEqual(reordered.payload["verdicts"], {"FIX": "eligible"})
        duplicate = await self.call(NAME, {"us_universe": ["brk-b", "BRK.B"]})
        self.assertFalse(duplicate.ok)
        self.assertIn("distinct list", duplicate.text)

    async def test_pre_event_quote_is_rejected_and_customer_filing_dates_supplier(self):
        customer_8k = self.source(
            "data_equity_filings",
            {
                "filings": [
                    {
                        "form": "8-K",
                        "filed": "2024-06-12T14:00:00Z",
                        "items": ["1.01 Entry into a Material Agreement"],
                    }
                ]
            },
        )
        old_quote = self.source(
            "data_market_quotes",
            {"quotes": {"SUPP": {"price": 10, "currency": "USD", "session_date": "2020-01-02"}}},
        )
        pre_same_day = self.source(
            "data_market_quotes",
            {
                "quotes": {
                    "SUPP": {
                        "price": 12,
                        "currency": "USD",
                        "as_of": "2024-06-12T10:00:00Z",
                        "session_date": "2024-06-12",
                    }
                }
            },
        )
        same_day_session = self.source(
            "data_market_quotes",
            {"quotes": {"SUPP": {"price": 15, "currency": "USD", "session_date": "2024-06-12"}}},
        )
        post_quote = self.source(
            "data_market_quotes",
            {
                "quotes": {
                    "SUPP": {
                        "price": 16,
                        "currency": "USD",
                        "as_of": "2024-06-12T15:00:00Z",
                        "session_date": "2024-06-12",
                    }
                }
            },
        )
        for qid, expr in (
            (old_quote, f"prev:{old_quote}.quotes.SUPP.price * 5"),
            (pre_same_day, f"prev:{pre_same_day}.quotes.SUPP.price * 5"),
            (same_day_session, f"prev:{same_day_session}.quotes.SUPP.price * 5"),
            (post_quote, f"prev:{post_quote}.quotes.SUPP.price * 5"),
        ):
            calc = await self.call("calc", {"expression": expr})
            self.assertTrue(calc.ok, calc.text)
            decision = self.decision(
                "SUPP",
                customer_8k,
                "filings[0].items[0]",
                verdict="eligible",
                publication={"evidence_id": customer_8k, "path": "filings[0].filed"},
                price_hurdle={
                    "quote_evidence": qid,
                    "calculation": {"evidence_id": calc.evidence_id, "path": "result"},
                    "assumptions": "Required market cap / 10x.",
                    "plausible_range": "50 to 90 USDm.",
                },
            )
            res = await self.call(NAME, {"us_universe": ["SUPP"], "decisions": [decision]})
            if qid in (old_quote, pre_same_day):
                self.assertFalse(res.ok)
                self.assertIn("predates the publication date", res.text)
            else:
                self.assertTrue(res.ok, res.text)
        watch = self.decision(
            "SUPP",
            customer_8k,
            "filings[0].items[0]",
            publication={"evidence_id": customer_8k, "path": "filings[0].filed"},
        )
        watched = await self.call(NAME, {"us_universe": ["SUPP"], "decisions": [watch]})
        self.assertTrue(watched.ok, watched.text)
        self.assertEqual(watched.payload["watch"], 1)

    async def test_clock_skew_and_naive_as_of_fallback(self):
        stamp = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1)
        ahead = stamp.isoformat().replace("+00:00", "Z")
        filing = self.source("data_equity_filings", {"filings": [{"form": "8-K", "filed": ahead}]})
        quotes = self.source(
            "data_market_quotes",
            {
                "quotes": {
                    "FIX": {
                        "price": 30,
                        "currency": "USD",
                        "as_of": "2024-06-13T10:00:00",
                        "session_date": "2999-01-01",
                    }
                }
            },
        )
        valid_date = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1)).date().isoformat()
        quotes_fallback = self.source(
            "data_market_quotes",
            {
                "quotes": {
                    "FIX": {
                        "price": 30,
                        "currency": "USD",
                        "as_of": "2024-06-13T10:00:00",
                        "session_date": valid_date,
                    }
                }
            },
        )
        calc = await self.call(
            "calc", {"expression": f"prev:{quotes_fallback}.quotes.FIX.price * 2"}
        )
        self.assertTrue(calc.ok, calc.text)
        bad_calc = await self.call("calc", {"expression": f"prev:{quotes}.quotes.FIX.price * 2"})
        self.assertTrue(bad_calc.ok, bad_calc.text)
        base = self.decision(
            "FIX",
            filing,
            "filings[0].form",
            verdict="eligible",
            publication={"evidence_id": filing, "path": "filings[0].filed"},
        )
        rejected = await self.call(
            NAME,
            {
                "us_universe": ["FIX"],
                "decisions": [
                    base
                    | {
                        "price_hurdle": {
                            "quote_evidence": quotes,
                            "calculation": {"evidence_id": bad_calc.evidence_id, "path": "result"},
                            "assumptions": "2x price.",
                            "plausible_range": "50 to 70.",
                        }
                    }
                ],
            },
        )
        self.assertFalse(rejected.ok)
        self.assertIn("dated, positive quote", rejected.text)
        accepted = await self.call(
            NAME,
            {
                "us_universe": ["FIX"],
                "decisions": [
                    base
                    | {
                        "price_hurdle": {
                            "quote_evidence": quotes_fallback,
                            "calculation": {"evidence_id": calc.evidence_id, "path": "result"},
                            "assumptions": "2x price.",
                            "plausible_range": "50 to 70.",
                        }
                    }
                ],
            },
        )
        self.assertTrue(accepted.ok, accepted.text)
        self.assertEqual(accepted.payload["decisions"][0]["valuation"]["quote_date"], valid_date)

    async def test_taxonomy_and_capabilities_classify_deferred_core_tool_consistently(self):
        self.assertEqual(classify_issues(self.registry, self.skills), [])
        cat = capabilities_catalog(self.registry, self.skills)
        self.assertNotIn(NAME, {item["name"] for item in cat["foundation"]})
        self.assertIn(NAME, {item["name"] for item in cat["extensions"]})
        for mode in MODES.values():
            rendered = render_capabilities(self.registry, self.skills, mode)
            self.assertIn(f"Other / extensions**: tools `{NAME}`", rendered)
