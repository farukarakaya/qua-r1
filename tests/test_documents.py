# SPDX-License-Identifier: Apache-2.0

import base64
import io
import json
import os
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI
from reportlab.pdfgen.canvas import Canvas

from trading_r1 import documents
from trading_r1.cli import documents as cli
from trading_r1.providers import available_backbones, client


def _pdf() -> bytes:
    data = io.BytesIO()
    canvas = Canvas(data)
    canvas.drawString(72, 700, "Forecast table")
    canvas.showPage()
    canvas.showPage()  # blank pages retain their physical page numbers
    canvas.save()
    return data.getvalue()


_PAGES = [{"page": 1, "markdown": "# FAVÖK\n\n| 2027T | 44,5 |"}, {"page": 2, "markdown": ""}]
_SUMMARY = "This report includes a FAVÖK forecast on page 1."


def _summary_response(summary: str = _SUMMARY, *, status: str = "completed") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "resp_summary_test",
            "status": status,
            "output": [
                {
                    "type": "message",
                    "id": "msg_summary_test",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps({"summary": summary}),
                            "annotations": [],
                        }
                    ],
                }
            ],
        },
    )


def _response(pages: list, *, status: str = "completed", summary: str = _SUMMARY) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "resp_ocr_test",
            "status": status,
            "output": [
                {
                    "type": "message",
                    "id": "msg_ocr_test",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps({"summary": summary, "pages": pages}),
                            "annotations": [],
                        }
                    ],
                }
            ],
        },
    )


class TestDocuments(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"TR1_HOME": str(self.home)}, clear=True))
        self.pdf = self.home / "report.pdf"
        self.pdf.write_bytes(_pdf())
        self.requests: list[httpx.Request] = []

    def _mock_openai(self, reply):
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return reply()

        transport = httpx.MockTransport(handle)
        self.enterContext(patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}))
        self.enterContext(
            patch.object(
                client,
                "AsyncOpenAI",
                side_effect=lambda **kwargs: AsyncOpenAI(
                    **kwargs, http_client=httpx.AsyncClient(transport=transport)
                ),
            )
        )

    def test_whole_pdf_one_request_and_reuse_by_contents(self):
        self._mock_openai(lambda: _response(_PAGES))
        with patch.dict(os.environ, {"TR1_GPT_OCR_MODEL": "profile-test-model"}):
            first = documents.add_pdf(self.pdf, model="vision-test-model")
        self.assertFalse(first.reused)
        same_pdf = self.home / "renamed.pdf"
        same_pdf.write_bytes(self.pdf.read_bytes())
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            second = documents.add_pdf(same_pdf)
        self.assertTrue(second.reused)
        self.assertEqual(first.path, second.path)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://api.openai.com/v1/responses")
        payload = json.loads(request.content)
        file = payload["input"][0]["content"][0]
        self.assertEqual(file["type"], "input_file")
        prefix, encoded = file["file_data"].split(",", 1)
        self.assertEqual(prefix, "data:application/pdf;base64")
        self.assertEqual(base64.b64decode(encoded), self.pdf.read_bytes())
        self.assertEqual(payload["model"], "vision-test-model")
        self.assertFalse(payload["store"])
        self.assertNotIn("tools", payload)
        self.assertNotIn("max_output_tokens", payload)
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertIn("summary", payload["text"]["format"]["schema"]["required"])
        self.assertEqual(stat.S_IMODE(first.path.stat().st_mode), 0o600)
        saved = json.loads(first.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["schema_version"], 2)
        self.assertEqual(saved["summary_prompt_id"], documents.SUMMARY_PROMPT_ID)
        self.assertEqual(saved["summary"], _SUMMARY)
        self.assertEqual(saved["pages"], _PAGES)
        self.assertEqual(saved["source_name"], "report.pdf")
        self.assertEqual(saved["sha256"], first.path.stem)
        self.assertEqual(saved["profile"], "gpt-ocr")
        self.assertEqual(saved["model"], "vision-test-model")

    def test_ocr_profile_does_not_inherit_chat_settings(self):
        self._mock_openai(lambda: _response(_PAGES))
        with patch.dict(
            os.environ,
            {
                "TR1_PROFILE": "gpt",
                "TR1_GPT_BASE_URL": "https://chat.example/v1",
                "TR1_GPT_MODEL": "chat-test-model",
                "TR1_GPT_API_KEY": "chat-key",
                "OPENAI_BASE_URL": "https://sdk.example/v1",
            },
        ):
            documents.add_pdf(self.pdf)
        (request,) = self.requests
        self.assertEqual(str(request.url), "https://api.openai.com/v1/responses")
        self.assertEqual(request.headers["Authorization"], "Bearer test-key")
        self.assertEqual(json.loads(request.content)["model"], "gpt-6.1-luna")
        self.assertNotIn("gpt-ocr", {row["name"] for row in available_backbones()})

    def test_ocr_profile_uses_its_own_config(self):
        self._mock_openai(lambda: _response(_PAGES))
        config = {
            "profiles": {"gpt-ocr": {"model": "file-model", "base_url": "https://ocr.example/v1"}}
        }
        (self.home / "providers.json").write_text(json.dumps(config), encoding="utf-8")
        with patch.dict(
            os.environ,
            {
                "TR1_GPT_OCR_MODEL": "env-model",
                "TR1_GPT_OCR_API_KEY": "ocr-key",
            },
        ):
            documents.add_pdf(self.pdf)
        (request,) = self.requests
        self.assertEqual(str(request.url), "https://ocr.example/v1/responses")
        self.assertEqual(request.headers["Authorization"], "Bearer ocr-key")
        payload = json.loads(request.content)
        self.assertEqual(payload["model"], "env-model")

    def test_custom_endpoint_requires_its_own_key_before_upload(self):
        self._mock_openai(lambda: _response(_PAGES))
        with (
            patch.dict(os.environ, {"TR1_GPT_OCR_BASE_URL": "https://ocr.example/v1"}),
            self.assertRaisesRegex(documents.DocumentError, "TR1_GPT_OCR_API_KEY"),
        ):
            documents.add_pdf(self.pdf)
        self.assertEqual(self.requests, [])

    def test_config_validation_does_not_print_credentials(self):
        secret = "private-test-value"
        config = {"profiles": {"gpt-ocr": {"api_key": secret}}}
        (self.home / "providers.json").write_text(json.dumps(config), encoding="utf-8")
        with redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(cli.main(["add", str(self.pdf)]), 1)
        self.assertIn("api_key", stderr.getvalue())
        self.assertNotIn(secret, stderr.getvalue())

    def test_missing_key_is_reported_before_upload(self):
        with (
            patch.object(client, "AsyncOpenAI", side_effect=AssertionError),
            self.assertRaisesRegex(documents.DocumentError, "secrets set OPENAI_API_KEY"),
        ):
            documents.add_pdf(self.pdf)
        self.assertEqual(list((self.home / "docs").glob("*.json")), [])

    def test_simultaneous_imports_only_send_once(self):
        self._mock_openai(lambda: _response(_PAGES))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(documents.add_pdf, [self.pdf, self.pdf]))
        self.assertEqual(sorted(result.reused for result in results), [False, True])
        self.assertEqual(len(self.requests), 1)

    def test_partial_or_incomplete_transcript_is_not_saved(self):
        replies = iter(
            [
                _response(_PAGES[:1]),
                _response(_PAGES, status="incomplete"),
                _response(_PAGES, summary="  "),
            ]
        )
        self._mock_openai(lambda: next(replies))
        for expected in (
            "every page in order",
            "did not finish",
            "did not return a report summary",
        ):
            with self.assertRaisesRegex(documents.DocumentError, expected):
                documents.add_pdf(self.pdf)
            self.assertEqual(list((self.home / "docs").glob("*.json")), [])
        self.assertEqual(len(self.requests), 3)

    def test_invalid_saved_transcript_does_not_trigger_another_upload(self):
        self._mock_openai(lambda: _response(_PAGES))
        imported = documents.add_pdf(self.pdf)
        imported.path.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(documents.DocumentError, "saved transcript is unreadable"):
            documents.add_pdf(self.pdf)
        self.assertEqual(len(self.requests), 1)

    def test_api_error_is_not_retried_or_saved(self):
        self._mock_openai(lambda: httpx.Response(500, json={"error": {"message": "private body"}}))
        with self.assertRaisesRegex(documents.DocumentError, "HTTP 500") as cm:
            documents.add_pdf(self.pdf)
        self.assertNotIn("private body", str(cm.exception))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(list((self.home / "docs").glob("*.json")), [])

    def test_cli_continues_a_batch_after_a_bad_file(self):
        self._mock_openai(lambda: _response(_PAGES))
        invalid = self.home / "invalid.pdf"
        invalid.write_text("Not a PDF")
        with redirect_stderr(io.StringIO()) as err, redirect_stdout(io.StringIO()) as stdout:
            code = cli.main(["add", str(invalid), str(self.pdf)])
        self.assertEqual(code, 1)
        self.assertIn("not a PDF", err.getvalue())
        self.assertIn("saved OCR", stdout.getvalue())
        self.assertEqual(len(self.requests), 1)

    def test_backfill_summaries_upgrades_v1_and_stale_v2_without_reuploading_pdf(self):
        self._mock_openai(lambda: _response(_PAGES))
        imported = documents.add_pdf(self.pdf)
        record = json.loads(imported.path.read_text(encoding="utf-8"))

        # Downgrade to schema_version 1 without summary and backfill.
        v1 = {k: v for k, v in record.items() if k not in ("summary", "summary_prompt_id")}
        v1["schema_version"] = 1
        imported.path.write_text(json.dumps(v1), encoding="utf-8")

        self.requests.clear()
        self._mock_openai(lambda: _summary_response("Backfilled v1 summary."))
        upgraded = documents.add_pdf(self.pdf, backfill=True)
        self.assertTrue(upgraded.reused)
        self.assertTrue(upgraded.backfilled)
        self.assertEqual(len(self.requests), 1)
        payload = json.loads(self.requests[0].content)
        self.assertEqual(payload["input"][0]["content"][0]["type"], "input_text")
        saved = json.loads(imported.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["schema_version"], 2)
        self.assertEqual(saved["summary"], "Backfilled v1 summary.")
        self.assertEqual(saved["summary_prompt_id"], documents.SUMMARY_PROMPT_ID)

        # Already up-to-date v2 transcript does not trigger another request.
        again = documents.add_pdf(self.pdf, backfill=True)
        self.assertTrue(again.reused)
        self.assertFalse(again.backfilled)
        self.assertEqual(len(self.requests), 1)

        # Stale summary_prompt_id triggers a summary refresh.
        saved["summary_prompt_id"] = "stale-prompt-id"
        imported.path.write_text(json.dumps(saved), encoding="utf-8")
        self._mock_openai(lambda: _summary_response("Refreshed v2 summary."))
        with redirect_stdout(io.StringIO()) as stdout:
            code = cli.main(["add", "--backfill-summaries", str(self.pdf)])
        self.assertEqual(code, 0)
        self.assertIn("added summary", stdout.getvalue())
        refreshed = json.loads(imported.path.read_text(encoding="utf-8"))
        self.assertEqual(refreshed["summary"], "Refreshed v2 summary.")
        self.assertEqual(refreshed["summary_prompt_id"], documents.SUMMARY_PROMPT_ID)

    def test_cli_scans_directories_skips_symlinks_and_validates_workers(self):
        self._mock_openai(lambda: _response(_PAGES))
        folder = self.home / "reports"
        nested = folder / "nested"
        nested.mkdir(parents=True)
        real_pdf = nested / "a.pdf"
        real_pdf.write_bytes(self.pdf.read_bytes())
        (folder / "notes.txt").write_text("ignored", encoding="utf-8")
        (folder / "link.pdf").symlink_to(real_pdf)
        empty = self.home / "empty"
        empty.mkdir()

        with redirect_stderr(io.StringIO()) as err, redirect_stdout(io.StringIO()) as stdout:
            code = cli.main(["add", "--workers", "2", str(folder), str(empty)])
        self.assertEqual(code, 1)
        self.assertIn("no PDFs found", err.getvalue())
        self.assertIn("saved OCR", stdout.getvalue())
        self.assertEqual(len(self.requests), 1)

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            cli.main(["add", "--workers", "0", str(self.pdf)])
        self.assertEqual(cm.exception.code, 2)
