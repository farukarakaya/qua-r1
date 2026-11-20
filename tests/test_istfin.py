# SPDX-License-Identifier: Apache-2.0

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from trading_r1.assembly import catalog_tool_registry
from trading_r1.contract.spec import ToolError
from trading_r1.providers import ModelProfile
from trading_r1.service import state
from trading_r1.service.routes.sessions import new_session, status
from trading_r1.service.syncing import SyncRuntime, is_synced_kind
from trading_r1.state.credentials import set_value
from trading_r1.sync.engine import SyncEngine
from trading_r1.tools.data.market import istfin


class IstfinTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.home = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(
            patch.dict(os.environ, {"TR1_HOME": self.home, "ISTFIN_API_KEY": "fixture"}, clear=True)
        )

    def test_new_chat_policy_uses_backend_key_only_at_creation(self):
        private = new_session()["id"]
        self.assertFalse(is_synced_kind(private))
        del os.environ["ISTFIN_API_KEY"]
        ordinary = new_session()["id"]
        self.assertTrue(is_synced_kind(ordinary))
        set_value("ISTFIN_API_KEY", "stored-fixture")
        self.assertTrue(new_session()["id"].startswith("istfin-"))
        self.assertTrue(is_synced_kind(ordinary))
        self.assertFalse(is_synced_kind(private))

    async def test_pdf_istfin_status_and_prompt_overhead_follow_the_backend_key(self):
        profile = ModelProfile(name="fixture", model="fixture", context_window=64_000)
        with (
            patch.object(state, "resolve_profile", return_value=profile),
            patch.object(state, "prompt_overhead", return_value=123) as overhead,
        ):
            for session_id, enabled in (("docs-abc", True), ("ordinary-abc", False)):
                with self.subTest(session_id=session_id):
                    current = await status(session_id)
                    self.assertEqual(current["istfin"], enabled)
                    self.assertEqual(overhead.call_args.kwargs["use_istfin"], enabled)
            del os.environ["ISTFIN_API_KEY"]
            self.assertFalse((await status("docs-abc"))["istfin"])
            self.assertFalse(overhead.call_args.kwargs["use_istfin"])
            set_value("ISTFIN_API_KEY", "stored-fixture")
            self.assertTrue((await status("docs-abc"))["istfin"])
            self.assertTrue(overhead.call_args.kwargs["use_istfin"])
        self.assertFalse(is_synced_kind("docs-abc"))

    def transport(self, respond):
        client = httpx.AsyncClient
        self.enterContext(
            patch.object(
                istfin.httpx,
                "AsyncClient",
                side_effect=lambda **kwargs: client(
                    transport=httpx.MockTransport(respond), **kwargs
                ),
            )
        )

    async def test_news_returns_cursor_canonical_link_and_correction(self):
        def respond(request):
            self.assertEqual(request.url.path, "/v1/news")
            self.assertEqual(request.url.params["symbols"], "TEST")
            self.assertEqual(request.url.params["from"], "2025-03-10")
            self.assertEqual(request.url.params["types"], "FR")
            self.assertEqual(request.url.params["cursor"], "830")
            item = {
                "id": 823,
                "symbols": ["TEST"],
                "type": "FR",
                "title": "Revised statements",
                "publishedAt": "2025-03-10T21:05:00Z",
                "isUpdate": True,
                "isCorrection": True,
            }
            return httpx.Response(
                200, json={"data": [item], "meta": {"hasMore": True, "nextCursor": "823"}}
            )

        self.transport(respond)
        r = await istfin._news(
            istfin.NewsRequest(
                symbols=["XIST:TEST", "TEST.IS"], types=["FR"], start="2025-03-10", cursor=830
            )
        )
        self.assertEqual(r.output.next_cursor, 823)
        (item,) = r.output.items
        self.assertTrue(item.is_correction)
        self.assertEqual(item.url, "https://www.kap.org.tr/tr/Bildirim/823")
        self.assertEqual(r.provenance.as_of, "2025-03-10T21:05:00Z")

    async def test_ratios_keep_reporting_quarter_on_each_day_and_missing_values(self):
        rows = [
            {
                "day": "2025-11-04",
                "values": {"pe": 10, "pb": 0},
                "asOfQuarter": "2025-09",
                "asOfSource": "kap",
            },
            {
                "day": "2025-11-03",
                "values": {"pe": 12},
                "asOfQuarter": "2025-06",
                "asOfSource": "kap",
            },
        ]
        self.transport(lambda _: httpx.Response(200, json={"data": rows, "meta": {}}))
        r = await istfin._ratios(istfin.RatiosRequest(symbol="TEST.IS"))
        self.assertEqual(
            [(x.day, x.as_of_quarter, x.values["pb"]) for x in r.output.rows],
            [("2025-11-03", "2025-06", None), ("2025-11-04", "2025-09", 0)],
        )
        self.assertEqual(r.provenance.as_of, "2025-11-04")

    def test_istfin_catalog_does_not_offer_retired_index_tool(self):
        self.assertIsNone(catalog_tool_registry(use_istfin=True).get("data_market_istfin_index"))

    async def test_quota_error_does_not_retry_or_echo_server_details(self):
        calls = []

        def respond(request):
            calls.append(request.url.path)
            return httpx.Response(
                429,
                json={"error": {"code": "QUOTA_EXCEEDED", "message": "private details"}},
                headers={"Retry-After": "600"},
            )

        self.transport(respond)
        with self.assertRaisesRegex(ToolError, "daily quota exhausted.*600 seconds") as err:
            await istfin._disclosure(istfin.DisclosureRequest(disclosure_id=823))
        self.assertNotIn("private details", str(err.exception))
        self.assertEqual(calls, ["/v1/news/823"])

    async def test_plan_limit_retries_once_with_same_filters_and_reports_window(self):
        calls = []

        def respond(request):
            calls.append(dict(request.url.params))
            if len(calls) == 1:
                return httpx.Response(403, json={"error": {"code": "PLAN_LIMIT_EXCEEDED"}})
            return httpx.Response(200, json={"data": [], "meta": {}})

        self.transport(respond)
        result = await istfin._news(
            istfin.NewsRequest(
                symbols=["XIST:TEST", "XIST:OTHER"],
                types=["CA"],
                query="cash dividend",
                start="2025-01-01",
                end="2025-04-30",
                limit=12,
            )
        )
        filters = {"symbols": "TEST,OTHER", "types": "CA", "q": "cash dividend", "limit": "12"}
        self.assertEqual(
            calls,
            [
                {**filters, "from": "2025-01-01", "to": "2025-04-30"},
                {**filters, "from": "2025-04-01", "to": "2025-04-30"},
            ],
        )
        self.assertEqual(
            result.output.coverage.model_dump(),
            {
                "narrowed": True,
                "queried_from": "2025-04-01",
                "queried_to": "2025-04-30",
            },
        )

    async def test_persistent_plan_limit_reports_remaining_error_without_third_request(self):
        calls = []

        def respond(request):
            calls.append(dict(request.url.params))
            return httpx.Response(403, json={"error": {"code": "PLAN_LIMIT_EXCEEDED"}})

        self.transport(respond)
        with self.assertRaisesRegex(ToolError, "2025-04-01 to 2025-04-30.*plan limit") as err:
            await istfin._ratios(istfin.RatiosRequest(symbol="TEST.IS", end="2025-04-30"))
        self.assertEqual(err.exception.code, "istfin_plan_limit")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1]["metrics"], calls[0]["metrics"])
        calls.clear()
        with self.assertRaisesRegex(ToolError, "plan limit"):
            await istfin._news(istfin.NewsRequest(start="2025-01-01", end="2025-04-30", cursor=823))
        self.assertEqual(len(calls), 1)

    async def test_auth_does_not_narrow_or_retry(self):
        calls = []

        def respond(request):
            calls.append(request.url.path)
            return httpx.Response(401, json={"error": {"code": "PLAN_LIMIT_EXCEEDED"}})

        self.transport(respond)
        with self.assertRaisesRegex(ToolError, "rejected the API key") as err:
            await istfin._news(istfin.NewsRequest(start="2025-01-01", end="2025-04-30"))
        self.assertEqual(err.exception.code, "istfin_auth")
        self.assertEqual(calls, ["/v1/news"])

    async def test_istfin_session_is_opt_in_and_cannot_be_uploaded(self):
        self.assertIsNone(catalog_tool_registry().get("data_market_istfin_ratios"))
        self.assertIsNotNone(
            catalog_tool_registry(use_istfin=True).get("data_market_istfin_ratios")
        )
        sessions = Path(self.home) / "sessions"
        sessions.mkdir()
        sid = "istfin-personal"
        (sessions / f"{sid}.jsonl").write_text("private research")
        sync = SyncRuntime(sessions, enabled=lambda: True)
        self.addAsyncCleanup(sync.aclose)
        self.assertFalse(sync.enrol(sid))
        self.assertEqual(sync.import_history()["enrolled"], 0)
        sync.store.enrol(sid, 0)
        engine = SyncEngine(
            sessions,
            "fixture",
            store=sync.store,
            transport=httpx.MockTransport(lambda _: self.fail("unexpected upload")),
        )
        self.addAsyncCleanup(engine.client.close)
        self.assertEqual(await engine.push(sid, force=True), "off")
