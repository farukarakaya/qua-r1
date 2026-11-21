# SPDX-License-Identifier: Apache-2.0

import os
import tempfile
import unittest
from unittest.mock import patch

from trading_r1.assembly import build_run_registry, builtin_skill_registry, catalog_tool_registry
from trading_r1.runtime.dispatch import dispatch
from trading_r1.runtime.evidence import EvidenceStore
from trading_r1.runtime.session import recorder
from trading_r1.runtime.session.store import SessionStore
from trading_r1.runtime.skill_registry import SkillSessionState
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
                for _ in range(2):
                    next_page = await self.call(
                        bist_screen.SPEC.name, {"cursor": page.payload["next_cursor"]}
                    )
                    self.assertTrue(next_page.ok, next_page.text)
            detach()
            rec.end(None, termination="answer")

            restored = EvidenceStore.from_list(SessionStore(home).evidence("istfin-fixture"))
            original = restored.get(saved.evidence_id)
            self.assertEqual(original.payload, saved.payload)
            self.assertTrue(original.recorded_at)
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
            self.assertEqual(
                SessionStore(home).evidence("istfin-fixture")[-1]["payload"]["verdicts"][
                    "XIST:SUPPL"
                ],
                "pass",
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
                    }
                ]
            },
        )
        filing = self.source(
            "data_equity_filings",
            {"filings": [{"form": "8-K", "report_date": "2024-06-01", "filed": "2999-06-12"}]},
        )
        watch = self.decision("FIX", undated, "items[0].title")
        cases = [
            (watch | {"early_claim": True}, "needs a recorded publication date"),
            (
                watch | {"publication": {"evidence_id": undated, "path": "items[0].published"}},
                "not a publication time",
            ),
            (
                watch | {"publication": {"evidence_id": filing, "path": "filings[0].report_date"}},
                "not a report or retrieval date",
            ),
            (
                watch | {"publication": {"evidence_id": filing, "path": "filings[0].filed"}},
                "in the future",
            ),
            (self.decision("FIX", "e999", "title"), "No discovery evidence e999"),
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
        coverage = await self.call(NAME, {"us_universe": ["FIX"]})
        self.assertTrue(coverage.ok, coverage.text)
        self.assertEqual(coverage.payload["examined"], 0)
        saved = await self.call(NAME, {"us_universe": ["FIX"], "decisions": [watch]})
        self.assertTrue(saved.ok, saved.text)
        self.assertIsNone(saved.payload["decisions"][0]["published_at"])
        self.assertEqual(saved.payload["watch"], 1)

    async def test_us_roster_and_eligible_hurdle_use_dated_quote_and_calculation(self):
        filing = self.source(
            "data_equity_filings", {"filings": [{"form": "8-K", "filed": "2024-06-12"}]}
        )
        quotes = self.source(
            "data_market_quotes",
            {"quotes": {"FIX": {"price": 30, "currency": "USD", "session_date": "2024-06-13"}}},
        )
        calculated = await self.call(
            "calc",
            {"values": {"p": f"prev:{quotes}.quotes.FIX.price"}, "expression": "p * 10 / 15"},
        )
        unlinked = await self.call("calc", {"expression": "30 * 10 / 15"})
        for result in (calculated, unlinked):
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
        invalid = decision | {
            "price_hurdle": decision["price_hurdle"]
            | {"calculation": {"evidence_id": unlinked.evidence_id, "path": "result"}}
        }
        failed = await self.call(NAME, req | {"decisions": [invalid]})
        self.assertFalse(failed.ok)
        self.assertIn("Chain the quote", failed.text)
        failed = await self.call(NAME, {"us_universe": ["PEER"], "decisions": [decision]})
        self.assertFalse(failed.ok)
        self.assertIn("on the supplied list", failed.text)
        saved = await self.call(NAME, req | {"decisions": [decision]})
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
        reordered = await self.call(NAME, {"us_universe": ["fix", "PEER"]})
        self.assertEqual(reordered.payload["verdicts"], {"FIX": "eligible"})
        duplicate = await self.call(NAME, {"us_universe": ["brk-b", "BRK.B"]})
        self.assertFalse(duplicate.ok)
        self.assertIn("distinct list", duplicate.text)
