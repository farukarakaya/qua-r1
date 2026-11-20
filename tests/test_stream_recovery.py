# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
from openai import APIError, AuthenticationError

from trading_r1.assembly import catalog_tool_registry
from trading_r1.providers.openai import OpenAIProvider, _turn_from_response
from trading_r1.providers.profiles import ModelProfile
from trading_r1.runtime.agent import Agent
from trading_r1.runtime.recovery import BACKOFF, FATAL, classify_model_error
from trading_r1.state.runtime_config import BackoffConfig, LimitsConfig, RuntimeConfig

_PROFILE = ModelProfile(name="fixture", model="fixture", provider="openai-responses")
_REQUEST = httpx.Request("POST", "https://example.test/v1/responses")
_BROKEN = (
    "stream error: stream disconnected before completion: stream closed before response.completed"
)


def api_error(message=_BROKEN):
    return APIError(message, _REQUEST, body=None)


class Stream:
    def __init__(self, output, *, error=None):
        self.output = output
        self.error = error

    async def __aiter__(self):
        if self.error:
            yield SimpleNamespace(type="response.function_call_arguments.delta", delta='{"ex')
            yield SimpleNamespace(type="response.output_text.delta", delta="discard this")

    async def get_final_response(self):
        if self.error:
            raise self.error
        return SimpleNamespace(
            status="completed",
            output=self.output,
            usage=None,
            output_text="" if self.output else "The answer is four.",
        )


class StreamRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_classifier_is_specific(self):
        provider = OpenAIProvider(_PROFILE)
        self.assertEqual(classify_model_error(api_error(), provider), BACKOFF)
        self.assertEqual(classify_model_error(api_error("invalid request"), provider), FATAL)
        self.assertEqual(classify_model_error(asyncio.CancelledError(), provider), FATAL)
        auth = AuthenticationError(
            _BROKEN, response=httpx.Response(401, request=_REQUEST), body=None
        )
        self.assertEqual(classify_model_error(auth, provider), FATAL)

    def test_terminal_incomplete_response_discards_unfinished_tool_item(self):
        response = SimpleNamespace(
            status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            output=[SimpleNamespace(type="function_call", arguments='{"expr')],
            output_text="",
        )
        turn = _turn_from_response(_PROFILE, response)
        self.assertEqual(turn.finish_reason, "length")
        self.assertEqual(turn.tool_calls, [])
        self.assertFalse(turn.is_actionable)

    async def run_fixture(self, failures):
        tool = SimpleNamespace(
            type="function_call",
            name="calc",
            arguments='{"expression":"2+2"}',
            call_id="call_once",
            model_dump=lambda **_: {
                "type": "function_call",
                "name": "calc",
                "call_id": "call_once",
                "arguments": '{"expression":"2+2"}',
            },
        )
        requests = []

        @asynccontextmanager
        async def stream(**kwargs):
            requests.append(kwargs["input"])
            attempt = len(requests)
            if attempt == 1:
                yield Stream([tool])
            elif attempt <= 1 + failures:
                yield Stream([], error=api_error())
            else:
                yield Stream([])

        agent = Agent(
            profile=_PROFILE,
            registry=catalog_tool_registry(),
            stream_tokens=True,
            config=RuntimeConfig(
                limits=LimitsConfig(max_transient_retries=2),
                backoff=BackoffConfig(base_delay_s=0, max_delay_s=0),
            ),
            client=SimpleNamespace(responses=SimpleNamespace(stream=stream)),
        )
        events = []
        result = await agent.run(
            "Calculate twice two.", on_event=lambda k, d: events.append((k, d))
        )
        for request in requests[1:]:
            self.assertEqual(json.dumps(request), json.dumps(requests[1]))
            self.assertEqual(sum(x.get("type") == "function_call_output" for x in request), 1)
        self.assertEqual(sum(k == "tool_call" for k, _ in events), 1)
        self.assertFalse(any(k == "ask_user" for k, _ in events))
        self.assertNotIn("discard this", str(events))
        return result, events, len(requests)

    async def test_disconnect_after_tool_retries_without_replaying_tool(self):
        _, events, count = await self.run_fixture(failures=1)
        self.assertEqual(count, 3)
        self.assertEqual(
            [d["content"] for k, d in events if k == "last_message"], ["The answer is four."]
        )
        self.assertFalse(any(k == "error" for k, _ in events))

    async def test_persistent_disconnect_uses_bounded_budget(self):
        _, events, count = await self.run_fixture(failures=4)
        self.assertEqual(count, 4)
        self.assertEqual(sum(k == "error" for k, _ in events), 1)
        self.assertFalse(any(k == "last_message" for k, _ in events))
