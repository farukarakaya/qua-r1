# SPDX-License-Identifier: Apache-2.0

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from trading_r1.providers.client import build_create_kwargs
from trading_r1.providers.openai import OpenAIProvider
from trading_r1.providers.profiles import ModelProfile, ProfileSelectionError, load_profiles


class ReasoningEffortTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_responses_reasoning_payload(self):
        cases = [
            (None, {"effort": "high", "summary": "detailed"}, ["reasoning.encrypted_content"]),
            (
                "medium",
                {"effort": "medium", "summary": "detailed"},
                ["reasoning.encrypted_content"],
            ),
            ("none", {"effort": "none"}, None),
        ]
        for effort, expected_reasoning, expected_include in cases:
            with self.subTest(effort=effort):
                create = AsyncMock(
                    return_value=SimpleNamespace(
                        status="completed", output=[], output_text="ok", usage=None
                    )
                )
                profile = ModelProfile(
                    name="gpt",
                    model="fixture-model",
                    provider="openai-responses",
                    reasoning_effort=effort,
                )
                provider = OpenAIProvider(
                    profile, client=SimpleNamespace(responses=SimpleNamespace(create=create))
                )
                await provider.generate([{"role": "user", "content": "hi"}], max_tokens=256)
                kwargs = create.call_args.kwargs
                self.assertEqual(kwargs["reasoning"], expected_reasoning)
                self.assertEqual(kwargs.get("include"), expected_include)

    def test_chat_completions_extra_body_reasoning_effort(self):
        unset = ModelProfile(
            name="plain",
            model="fixture",
            extra_body={"top_k": 20},
        )
        unset_kwargs = build_create_kwargs(
            unset, [{"role": "user", "content": "hi"}], max_tokens=256
        )
        self.assertEqual(unset_kwargs["extra_body"], {"top_k": 20})

        override = ModelProfile(
            name="ollama-reader",
            model="gemma4:e4b",
            extra_body={"top_k": 20, "reasoning_effort": "none"},
            reasoning_effort="low",
        )
        override_kwargs = build_create_kwargs(
            override, [{"role": "user", "content": "hi"}], max_tokens=256
        )
        self.assertEqual(override_kwargs["extra_body"], {"top_k": 20, "reasoning_effort": "low"})

    def test_invalid_reasoning_effort_raises_profile_selection_error(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "providers.json"
            for env in (
                {"QUA_REASONING_EFFORT": "turbo"},
                {"TR1_GPT_REASONING_EFFORT": "turbo"},
            ):
                with self.subTest(env=env), self.assertRaises(ProfileSelectionError):
                    load_profiles(path=missing, env=env)
