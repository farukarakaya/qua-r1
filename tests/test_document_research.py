# SPDX-License-Identifier: Apache-2.0

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
from trading_r1.service.routes.research import ResearchRequest, _run_turn
from trading_r1.service.syncing import SyncRuntime
from trading_r1.state.documents import catalog_context
from trading_r1.state.fileio import write_json
from trading_r1.sync.engine import SyncEngine
from trading_r1.tools.core.read_doc import SPEC

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

    def _save(self, id_, *, summary=None):
        data = {
            "sha256": id_,
            "source_name": "Broker report.pdf",
            "schema_version": 1 if summary is None else 2,
            "pages": [
                {"page": 1, "markdown": "Company background"},
                {"page": 2, "markdown": _PAGE},
            ],
        }
        if summary is not None:
            data["summary"] = summary
        write_json(self.home / "docs" / f"{id_}.json", data, private=True)

    async def test_broad_question_gets_summaries_and_cites_only_pages_it_reads(self):
        requests = []
        tool_use = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_doc",
                    "type": "function",
                    "function": {
                        "name": "read_doc",
                        "arguments": json.dumps({"document_id": _DOC_ID, "start_page": 2}),
                    },
                }
            ],
        }

        def respond(request):
            requests.append((request.url.path, json.loads(request.content)))
            if len(requests) == 1:
                return _chat_reply(tool_use, "tool_calls")
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

        self.assertEqual([path for path, _ in requests], ["/v1/chat/completions"] * 3)
        first, after_read, ordinary = (request for _, request in requests)
        self.assertIn(_QUESTION, first["messages"][-1]["content"])
        self.assertIn(_SUMMARY, first["messages"][-1]["content"])
        self.assertNotIn(_PAGE, json.dumps(first["messages"]))
        self.assertIn("read_doc", {t["function"]["name"] for t in first["tools"]})
        self.assertIn(_PAGE, after_read["messages"][-1]["content"])
        self.assertNotIn("Company background", after_read["messages"][-1]["content"])
        self.assertIn("Buy [1]", result["text"])
        reference = "## References\n\n- [1] Broker report.pdf, p. 2\n"
        self.assertIn(reference, result["text"])
        self.assertIn(reference, snapshot_from_session(self.sessions, session_id).markdown())
        (citation,) = result["citations"]
        self.assertEqual(citation["record"], result["tool_log"][0]["evidence"])
        self.assertEqual(self.sessions.history(session_id)[0]["content"], _QUESTION)
        self.assertNotIn(_SUMMARY, json.dumps(ordinary["messages"]))
        self.assertNotIn("read_doc", {t["function"]["name"] for t in ordinary["tools"]})
        self.assertIsNone(self.sync.store.get(session_id))
        self.assertIsNotNone(self.sync.store.get("ordinary-test"))

    async def test_legacy_transcript_is_listed_and_can_be_read(self):
        legacy_id = "b" * 64
        self._save(legacy_id)
        self.assertIn("No summary. The pages are available.", catalog_context())
        registry = ToolRegistry()
        registry.register(SPEC)
        read = await dispatch("read_doc", {"document_id": legacy_id}, registry=registry)
        self.assertTrue(read.ok)
        self.assertEqual([p["page"] for p in read.payload["pages"]], [1, 2])
        for args in ({"document_id": "../providers"}, {"document_id": legacy_id, "start_page": 3}):
            with self.subTest(args=args):
                rejected = await dispatch("read_doc", args, registry=registry)
                self.assertFalse(rejected.ok)
                self.assertNotIn(_PAGE, rejected.text)

    def test_catalog_discloses_when_not_all_summaries_fit(self):
        for i in range(12):
            self._save(f"{i:064x}", summary="A report summary. " * 40)
        header, *rows = catalog_context().splitlines()
        self.assertIn(f"Showing {len(rows)} of 13 saved reports", header)
        self.assertLess(len(rows), 13)
        self.assertIn("Catalog is partial", header)

    async def test_sync_skips_document_sessions_even_if_already_enrolled(self):
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
