# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from trading_r1.providers import ModelProfile
from trading_r1.report import snapshot_from_session
from trading_r1.runtime.dispatch import dispatch
from trading_r1.runtime.memory import MemoryStore
from trading_r1.runtime.session.store import SessionStore
from trading_r1.runtime.tool_registry import ToolRegistry
from trading_r1.service import state
from trading_r1.service.app import create_app
from trading_r1.service.routes.research import ResearchRequest, _run_turn
from trading_r1.service.syncing import SyncRuntime
from trading_r1.state import documents
from trading_r1.state.documents import catalog_context, search_transcripts
from trading_r1.state.fileio import write_json
from trading_r1.sync.engine import SyncEngine
from trading_r1.tools.core.read_doc import SPEC as READ_DOC_SPEC
from trading_r1.tools.core.search_docs import SPEC as SEARCH_DOCS_SPEC

_DOC_ID = "a" * 64
_QUESTION = "Which Turkish stocks have the strongest analyst recommendations?"
_SUMMARY = "Example Brokerage covers XIST:ATATP; its recommendation is on page 2."
_PAGE = "Tavsiye: AL. Hedef fiyat: 125 TL."
_PROFILE = ModelProfile(
    name="fixture", model="fixture", reader_profile="gpt-reader", context_window=64_000
)


def _chat_reply(message, finish_reason="stop"):
    return httpx.Response(
        200,
        json={
            "id": "chat-fixture",
            "model": "fixture",
            "created": 0,
            "object": "chat.completion",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        },
    )


class DocumentResearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"TR1_HOME": str(self.home)}, clear=True))
        self.sessions = SessionStore(self.home / "sessions")
        self.sync = SyncRuntime(self.sessions.directory, enabled=lambda: True, access=lambda: None)
        self.addAsyncCleanup(self.sync.aclose)
        self.enterContext(
            patch.multiple(
                state,
                SESSIONS=self.sessions,
                SYNC=self.sync,
                MEMORY=MemoryStore(self.home / "memory"),
            )
        )
        self._save(_DOC_ID, summary=_SUMMARY)

    def _save(
        self, id_, *, summary=None, name="Broker report.pdf", pages=("Company background", _PAGE)
    ):
        data = {
            "sha256": id_,
            "source_name": name,
            "schema_version": 1 if summary is None else 2,
            "pages": [{"page": i, "markdown": text} for i, text in enumerate(pages, 1)],
        }
        if summary is not None:
            data["summary"] = summary
        write_json(self.home / "docs" / f"{id_}.json", data, private=True)

    async def test_broad_question_searches_first_and_cites_only_pages_it_reads(self):
        requests = []

        def tool_use(name, args):
            return _chat_reply(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": f"call_{name}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                },
                "tool_calls",
            )

        def respond(request):
            requests.append((request.url.path, json.loads(request.content)))
            if len(requests) == 1:
                return tool_use("search_docs", {"query": "tavsiye"})
            if len(requests) == 2:
                return tool_use(
                    "read_doc",
                    {
                        "document_id": "prev:e1.hits[0].document_id",
                        "start_page": "prev:e1.hits[0].matched_pages[0]",
                        "end_page": "prev:e1.hits[0].matched_pages[0]",
                    },
                )
            return _chat_reply(
                {"role": "assistant", "content": "Example Brokerage rated ATATP Buy [1]."}
            )

        async with AsyncOpenAI(
            api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        ) as client:
            session_id = "docs-test"
            result = await _run_turn(
                ResearchRequest(question=_QUESTION, session_id=session_id), _PROFILE, client=client
            )
            await _run_turn(
                ResearchRequest(question=_QUESTION, session_id="ordinary-test"),
                _PROFILE,
                client=client,
            )

        self.assertEqual([path for path, _ in requests], ["/v1/chat/completions"] * 4)
        first, after_search, after_read, ordinary = (request for _, request in requests)
        self.assertIn(_QUESTION, first["messages"][-1]["content"])
        self.assertIn("Broker report.pdf", first["messages"][-1]["content"])
        self.assertNotIn(_SUMMARY, json.dumps(first["messages"]))
        self.assertNotIn(_PAGE, json.dumps(first["messages"]))
        self.assertNotIn("data_market_istfin_ratios", first["messages"][0]["content"])
        self.assertTrue(
            {"read_doc", "search_docs"} <= {t["function"]["name"] for t in first["tools"]}
        )
        self.assertIn(_SUMMARY, after_search["messages"][-1]["content"])
        self.assertNotIn(_PAGE, json.dumps(after_search["messages"]))
        self.assertIn(_PAGE, after_read["messages"][-1]["content"])
        self.assertNotIn("Company background", after_read["messages"][-1]["content"])
        self.assertIn("Buy [1]", result["text"])
        reference = "## References\n\n- [1] Broker report.pdf, p. 2\n"
        self.assertIn(reference, result["text"])
        self.assertIn(reference, snapshot_from_session(self.sessions, session_id).markdown())
        (citation,) = result["citations"]
        self.assertEqual(citation["record"], result["tool_log"][1]["evidence"])
        self.assertEqual(self.sessions.history(session_id)[0]["content"], _QUESTION)
        self.assertNotIn(_SUMMARY, json.dumps(ordinary["messages"]))
        self.assertTrue(
            {"read_doc", "search_docs"}.isdisjoint(t["function"]["name"] for t in ordinary["tools"])
        )
        self.assertIsNone(self.sync.store.get(session_id))
        self.assertIsNotNone(self.sync.store.get("ordinary-test"))

    async def test_legacy_transcript_is_listed_and_can_be_read(self):
        legacy_id = "b" * 64
        self._save(legacy_id)
        reports = search_transcripts().hits
        self.assertIsNone(next(hit.summary for hit in reports if hit.document_id == legacy_id))
        registry = ToolRegistry()
        registry.register(READ_DOC_SPEC)
        read = await dispatch("read_doc", {"document_id": legacy_id}, registry=registry)
        self.assertTrue(read.ok)
        self.assertEqual([p["page"] for p in read.payload["pages"]], [1, 2])
        for args in ({"document_id": "../providers"}, {"document_id": legacy_id, "start_page": 3}):
            with self.subTest(args=args):
                rejected = await dispatch("read_doc", args, registry=registry)
                self.assertFalse(rejected.ok)
                self.assertNotIn(_PAGE, rejected.text)

    def test_large_catalog_is_bounded_but_search_covers_the_full_library(self):
        for i in reversed(range(60)):
            self._save(
                f"{i:064x}",
                name=f"LOGO_{i:02}.pdf",
                summary="A report summary. " * 40,
            )
        catalog = catalog_context()
        header, instruction, *rows = catalog.splitlines()
        self.assertIn("61 saved reports", header)
        self.assertIn("Showing 20 of 61", header)
        self.assertIn("search_docs", instruction)
        self.assertNotIn("A report summary", catalog)
        self.assertLess(len(catalog.encode()), 4_000)
        self.assertEqual(rows, sorted(rows))
        self.assertNotIn("LOGO_59.pdf", catalog)
        self.assertEqual(
            search_transcripts(tickers=["LOGO"], offset=59).hits[0].source_name, "LOGO_59.pdf"
        )

    async def test_ticker_prefix_and_tool_pagination(self):
        self._save("b" * 64, name="LOGO.pdf", pages=("Logo company profile",))
        self._save("c" * 64, name="HTTBT_2026.pdf", pages=("HTTBT earnings",))
        self._save("d" * 64, name="LOGON_2026.pdf", pages=("LOGO is a peer",))
        registry = ToolRegistry()
        registry.register(SEARCH_DOCS_SPEC)
        args = {"tickers": ["xist:logo", "HTTBT.IS"], "limit": 1}
        first = await dispatch("search_docs", args, registry=registry)
        self.assertTrue(first.ok, first.text)
        self.assertEqual(first.payload["total_matched"], 2)
        self.assertEqual(first.payload["hits"][0]["source_name"], "HTTBT_2026.pdf")
        self.assertEqual(first.payload["hits"][0]["matched_pages"], [1])
        second = await dispatch(
            "search_docs", args | {"offset": first.payload["next_offset"]}, registry=registry
        )
        self.assertEqual(second.payload["hits"][0]["source_name"], "LOGO.pdf")
        self.assertIsNone(second.payload["next_offset"])
        self.assertEqual(search_transcripts(query="HTTBT_202").hits[0].document_id, "c" * 64)

    def test_keyword_ranking_and_page_matches(self):
        self._save("b" * 64, summary="Net cash peers", pages=("No details", "NET CASH BACKLOG"))
        self._save(
            "c" * 64, summary="Net cash backlog for the year", pages=("The backlog doubled",)
        )
        self._save("d" * 64, summary="Cash " * 40, pages=("Netting cashback and backlogs",))
        ranked = search_transcripts("net cash backlog")
        self.assertEqual([hit.document_id for hit in ranked.hits], ["c" * 64, "b" * 64, "d" * 64])
        self.assertEqual([hit.matched_pages for hit in ranked.hits], [[1], [2], []])
        summaries = search_transcripts("backlog", scope="summary")
        self.assertEqual([hit.document_id for hit in summaries.hits], ["c" * 64])
        self.assertEqual(summaries.hits[0].matched_pages, [])

    def test_whitespace_query_behaves_like_omitted_query(self):
        self._save("b" * 64, name="LOGO_2025.pdf")
        for tickers in (None, ["LOGO"]):
            with self.subTest(tickers=tickers):
                self.assertEqual(
                    search_transcripts(query="   ", tickers=tickers),
                    search_transcripts(tickers=tickers),
                )

    def test_scan_cache_reuses_parsed_transcripts_and_checks_for_replacement_and_chmod(self):
        path = self.home / "docs" / f"{_DOC_ID}.json"
        with patch.object(
            documents, "read_private_json", wraps=documents.read_private_json
        ) as read:
            self.assertIn("Broker report.pdf", catalog_context())
            self.assertEqual(search_transcripts().hits[0].summary, _SUMMARY)
            self.assertEqual(read.call_count, 1)
            self._save(_DOC_ID, name="Updated.pdf", summary="Updated summary")
            self.assertIn("Updated.pdf", catalog_context())
            self.assertEqual(search_transcripts().hits[0].summary, "Updated summary")
            self.assertEqual(read.call_count, 2)
            path.chmod(0o644)
            self.assertNotIn("Updated.pdf", catalog_context())
            self.assertEqual(search_transcripts().omitted, 1)
            self.assertEqual(read.call_count, 3)
        path.unlink()
        self.assertEqual(search_transcripts().omitted, 0)

    def test_invalid_and_oversized_transcripts_are_omitted(self):
        malformed = self.home / "docs" / f"{'b' * 64}.json"
        malformed.write_text("not JSON", encoding="utf-8")
        malformed.chmod(0o600)
        self._save("c" * 64, pages=("spans the search byte budget" * 100,))
        with patch("trading_r1.state.documents._SCAN_MAX_BYTES", 1_200):
            self.assertIn("2 unreadable or oversized transcript(s) omitted", catalog_context())
            found = search_transcripts()
        self.assertEqual(found.omitted, 2)
        self.assertEqual([hit.document_id for hit in found.hits], [_DOC_ID])

    async def test_key_enables_istfin_tools_in_pdf_session_without_sync(self):
        with patch.dict(os.environ, {"ISTFIN_API_KEY": "fixture"}):
            requests = []

            def respond(request):
                body = json.loads(request.content)
                requests.append(body)
                if len(requests) == 1:
                    return _chat_reply(
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "load_ratios",
                                    "type": "function",
                                    "function": {
                                        "name": "load_tool",
                                        "arguments": json.dumps(
                                            {"tools": ["data_market_istfin_ratios"]}
                                        ),
                                    },
                                }
                            ],
                        },
                        "tool_calls",
                    )
                return _chat_reply({"role": "assistant", "content": "Done."})

            async with AsyncOpenAI(
                api_key="test",
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
            ) as client:
                await _run_turn(
                    ResearchRequest(question=_QUESTION, session_id="docs-combined"),
                    _PROFILE,
                    client=client,
                )
                await _run_turn(
                    ResearchRequest(question=_QUESTION, session_id="ordinary-existing"),
                    _PROFILE,
                    client=client,
                )
        self.assertIn("data_market_istfin_ratios", requests[0]["messages"][0]["content"])
        names = {t["function"]["name"] for t in requests[1]["tools"]}
        self.assertTrue({"search_docs", "read_doc", "data_market_istfin_ratios"} <= names)
        self.assertNotIn("data_market_istfin_ratios", requests[2]["messages"][0]["content"])
        self.assertIsNone(self.sync.store.get("docs-combined"))
        self.assertIsNotNone(self.sync.store.get("ordinary-existing"))

    async def test_toggle_in_place_persists_and_fences_an_in_progress_upload(self):
        session_id = "ordinary-existing"
        requests = []
        append_started = asyncio.Event()
        finish_append = asyncio.Event()
        uploaded = []

        def model(request):
            payload = json.loads(request.content)
            requests.append(payload)
            if len(requests) == 2:
                return _chat_reply(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_pdf",
                                "type": "function",
                                "function": {
                                    "name": "read_doc",
                                    "arguments": json.dumps(
                                        {"document_id": _DOC_ID, "start_page": 2, "end_page": 2}
                                    ),
                                },
                            }
                        ],
                    },
                    "tool_calls",
                )
            answers = {
                1: "Initial answer",
                3: "Report figure: 125 TL",
                4: "Follow-up",
                5: "Re-enabled",
            }
            return _chat_reply({"role": "assistant", "content": answers[len(requests)]})

        async def archive(request):
            if request.method == "GET":
                return httpx.Response(200, json={"data": []})
            if request.method == "PUT":
                self.assertFalse(uploaded, "updated metadata after opting out")
                return httpx.Response(200, json={"size": 0, "metadata": {}})
            self.assertEqual(request.method, "PATCH")
            uploaded.append(request.content)
            append_started.set()
            await finish_append.wait()
            size = int(request.headers["Upload-Offset"]) + len(request.content)
            return httpx.Response(200, json={"size": size, "verified_to": size})

        self.enterContext(patch.dict(os.environ, {"TR1_API_AUTH": "off"}))
        archive_sync = SyncRuntime(
            self.sessions.directory,
            enabled=lambda: True,
            access=lambda: ("test", "https://archive.example"),
            transport=httpx.MockTransport(archive),
            store=self.sync.store,
        )
        self.enterContext(patch.object(state, "SYNC", archive_sync))
        engine = archive_sync.engine()
        self.addAsyncCleanup(engine.client.close)
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app()), base_url="http://localhost"
            ) as api,
            AsyncOpenAI(
                api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(model))
            ) as client,
        ):
            path = f"/sessions/{session_id}"
            turn = ResearchRequest(session_id=session_id, question=_QUESTION)
            await _run_turn(turn, _PROFILE, client=client)
            self.assertTrue(self.sync.store.get(session_id))
            push = asyncio.create_task(archive_sync.push_now())
            try:
                await asyncio.wait_for(append_started.wait(), timeout=5)
                result = await api.put(f"{path}/pdfs", json={"enabled": True})
                self.assertEqual(result.json(), {"pdfs_enabled": True, "pdfs_private": True})
                await _run_turn(turn, _PROFILE, client=client)
                result = await api.put(f"{path}/pdfs", json={"enabled": False})
                self.assertEqual(result.json(), {"pdfs_enabled": False, "pdfs_private": True})
            finally:
                finish_append.set()
                await push

            # Recreate the session store, as on backend restart, before resuming the same id.
            with patch.object(state, "SESSIONS", SessionStore(self.sessions.directory)):
                saved = (await api.get(path)).json()
                self.assertFalse(saved["pdfs_enabled"])
                self.assertTrue(saved["pdfs_private"])
                self.assertEqual(len(saved["turns"]), 2)
                await _run_turn(turn, _PROFILE, client=client)
                await api.put(f"{path}/pdfs", json={"enabled": True})
                await _run_turn(turn, _PROFILE, client=client)

            available = [
                {t["function"]["name"] for t in r["tools"]} & {"search_docs", "read_doc"}
                for r in (requests[0], requests[1], requests[3], requests[4])
            ]
            both = {"search_docs", "read_doc"}
            self.assertEqual(available, [set(), both, set(), both])
            self.assertIn(_PAGE, requests[2]["messages"][-1]["content"])
            self.assertNotIn("Broker report.pdf", requests[3]["messages"][-1]["content"])
            self.assertIn("Report figure: 125 TL", json.dumps(requests[3]["messages"]))
            self.assertEqual(engine.state_of(session_id), "off")
            self.assertEqual(archive_sync.summary()["counts"]["off"], 1)
            self.assertFalse(archive_sync.enrol(session_id))
            self.assertEqual(archive_sync.import_history(), {"enrolled": 0, "already": 0})
            await archive_sync.run_pass()
            await archive_sync.push_now()
            self.assertEqual(len(uploaded), 1)

    async def test_reused_id_after_delete_requires_new_pdf_opt_in_and_stays_private(self):
        self.enterContext(patch.dict(os.environ, {"TR1_API_AUTH": "off"}))
        sid = "reused-id"
        path = f"/sessions/{sid}"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://localhost"
        ) as api:
            (await api.put(f"{path}/pdfs", json={"enabled": True})).raise_for_status()
            self.sessions.begin(sid, "Old question", {})
            (await api.delete(path)).raise_for_status()
            self.assertFalse(self.sessions.exists(sid))

            self.sessions.begin(sid, "New question", {})
            saved = (await api.get(path)).json()
            self.assertEqual([turn["question"] for turn in saved["turns"]], ["New question"])
            self.assertFalse(saved["pdfs_enabled"])
            self.assertTrue(saved["pdfs_private"])
            self.assertFalse(self.sync.enrol(sid))

    async def test_pdf_defaults_validation_and_active_turn(self):
        self.enterContext(patch.dict(os.environ, {"TR1_API_AUTH": "off"}))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://localhost"
        ) as api:
            path = "/sessions/docs-legacy"
            self.assertEqual(
                (await api.get(f"{path}/pdfs")).json(), {"pdfs_enabled": True, "pdfs_private": True}
            )
            async with state.session_lock("docs-legacy"):
                response = await api.put(f"{path}/pdfs", json={"enabled": False})
                self.assertEqual(response.status_code, 409)
            await api.put(f"{path}/pdfs", json={"enabled": False})
            self.assertEqual(
                (await api.get(f"{path}/pdfs")).json(),
                {"pdfs_enabled": False, "pdfs_private": True},
            )
            ordinary = await api.put("/sessions/ordinary/pdfs", json={"enabled": False})
            self.assertEqual(ordinary.json(), {"pdfs_enabled": False, "pdfs_private": False})
            invalid = await api.put("/sessions/INVALID/pdfs", json={"enabled": True})
            self.assertEqual(invalid.status_code, 422)

    async def test_sync_skips_document_sessions_even_if_already_enrolled(self):
        self.enterContext(patch.dict(os.environ, {"ISTFIN_API_KEY": "fixture"}))
        session_id = "docs-test"
        path = self.sessions.directory / f"{session_id}.jsonl"
        path.parent.mkdir()
        path.write_text(_SUMMARY, encoding="utf-8")
        self.assertEqual(self.sync.import_history()["enrolled"], 0)
        self.sync.store.enrol(session_id, 0)  # a stale enrolment cannot allow a push

        def unexpected(_request):
            self.fail("document session made an archive request")

        engine = SyncEngine(
            self.sessions.directory,
            "test",
            store=self.sync.store,
            transport=httpx.MockTransport(unexpected),
        )
        self.addAsyncCleanup(engine.client.close)
        self.assertEqual(await engine.push(session_id, force=True), "off")
