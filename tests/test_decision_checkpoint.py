# SPDX-License-Identifier: Apache-2.0

import json
import tempfile
import unittest
from unittest.mock import AsyncMock

from pydantic import BaseModel

from trading_r1.assembly import catalog_tool_registry
from trading_r1.contract.spec import Output, Result, ToolSpec
from trading_r1.contract.units import Text
from trading_r1.providers.base import AssistantTurn, ToolCall
from trading_r1.providers.profiles import ModelProfile
from trading_r1.runtime.agent import Agent
from trading_r1.runtime.evidence import EvidenceStore
from trading_r1.runtime.session import recorder
from trading_r1.runtime.session.store import SessionStore


class FilingRequest(BaseModel):
    section: int


class FilingOutput(Output):
    excerpt: Text


async def filing(req: FilingRequest) -> Result:
    text = "\n".join(
        f"Synthetic filing section {req.section}, note {n}: cash generation remains uncertain."
        for n in range(100)
    )
    return Result(FilingOutput(excerpt=text))


def turn(*specs):
    return AssistantTurn(
        finish_reason="tool_calls",
        tool_calls=[
            ToolCall(id=id_, name=name, arguments=json.dumps(args)) for id_, name, args in specs
        ],
    )


class CheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_offline_replay_retains_claims_and_halves_final_input(self):
        async def replay(store, with_checkpoint):
            rec = store.begin("fixture", "Review this portfolio", {})
            evidence = EvidenceStore()
            evidence.observe(rec.record_evidence)
            registry = catalog_tool_registry()
            registry.register(
                ToolSpec(
                    name="fixture_filing",
                    description="Read a synthetic filing section for the offline context replay.",
                    display_name="core.fixture_filing",
                    result_kind="data",
                    tier="core",
                    request_schema=FilingRequest,
                    output=FilingOutput,
                    handler=filing,
                )
            )
            agent = Agent(
                profile=ModelProfile(name="mock", model="mock"),
                registry=registry,
                evidence=evidence,
            )
            notes = {
                "mandate": "Hold unless thesis breaks",
                "holdings": ["HOLDCO: 2 shares"],
                "quote_date": "2024-01-02",
                "fx_date": "2024-01-01",
                "facts": [
                    {
                        "fact": "Quote 45",
                        "evidence_id": "e1",
                        "path": "result.price",
                        "citations": [1],
                    }
                ],
                "eligible_actions": ["Hold"],
                "unresolved": ["NAV unknown; no sale authorized"],
                "next_calculation": "Mark the two shares",
            }
            plan = {"steps": [{"step": "Reconcile the position", "status": "in_progress"}]}
            if with_checkpoint:
                plan["decision"] = notes
            first = turn(
                ("price", "calc", {"expression": '{"price":45}'}), ("plan", "update_plan", plan)
            )
            responses = [first]
            for i in range(5):
                responses.append(turn((f"section{i}", "fixture_filing", {"section": i})))
            responses.append(
                turn(
                    ("mark", "calc", {"values": {"q": "prev:e1.result.price"}, "expression": "q*2"})
                )
            )
            inputs = []

            async def generate(messages, **_):
                inputs.append(json.loads(json.dumps(messages)))
                if responses:
                    return responses.pop(0)
                return AssistantTurn(
                    content="Two shares are worth 90 [1]. NAV is unknown.", finish_reason="stop"
                )

            agent.provider.generate = AsyncMock(side_effect=generate)
            with recorder.bind(rec):
                result = await agent.run("Review this portfolio")
            rec.end(result)
            return inputs[-1], result, evidence

        with tempfile.TemporaryDirectory() as temp:
            baseline, result_a, a = await replay(SessionStore(f"{temp}/off"), False)
            compact, result_b, b = await replay(SessionStore(f"{temp}/on"), True)
            # Exactly the same recorded compute results and no fresh evidence retrieval.
            self.assertEqual(a.payloads, b.payloads)
            self.assertEqual(b.get("e7").payload["result"], 90)
            self.assertEqual(len(result_a["tool_log"]), len(result_b["tool_log"]))
            self.assertNotIn("evidence", {t["name"] for t in result_b["tool_log"]})
            self.assertEqual(result_a["messages"][-1], result_b["messages"][-1])
            self.assertEqual(
                [m["tool_call_id"] for m in baseline if m["role"] == "tool"],
                [m["tool_call_id"] for m in compact if m["role"] == "tool"],
            )

            def wire(messages):
                return len(json.dumps(messages, ensure_ascii=False).encode())

            self.assertLess(wire(compact), wire(baseline) / 2)

            reopened = SessionStore(f"{temp}/on")
            rec = reopened.begin("fixture", "Continue", {})
            checkpoint = rec.load_checkpoint()
            self.assertIsNotNone(checkpoint)
            self.assertEqual(checkpoint.notes.quote_date, "2024-01-02")
            self.assertEqual(checkpoint.notes.fx_date, "2024-01-01")
            self.assertEqual(checkpoint.notes.unresolved, ["NAV unknown; no sale authorized"])
            fact = checkpoint.notes.facts[0]
            restored = EvidenceStore.from_list(reopened.evidence("fixture"))
            self.assertEqual(
                (fact.evidence_id, fact.path, fact.citations), ("e1", "result.price", [1])
            )
            self.assertEqual(restored.get(fact.evidence_id).payload["result"]["price"], 45)
            self.assertFalse(
                any(m.get("type") == "decision.checkpoint" for m in reopened.lines("fixture"))
            )
