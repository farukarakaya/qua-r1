# SPDX-License-Identifier: Apache-2.0

import json
import tempfile
import unittest
from dataclasses import asdict
from unittest.mock import AsyncMock

from pydantic import BaseModel

from trading_r1.assembly import catalog_tool_registry
from trading_r1.contract.spec import Output, Result, ToolSpec
from trading_r1.contract.units import Text
from trading_r1.providers.base import AssistantTurn, ToolCall
from trading_r1.providers.profiles import ModelProfile
from trading_r1.runtime.agent import Agent
from trading_r1.runtime.checkpoint import DecisionCheckpoint, DecisionNotes
from trading_r1.runtime.evidence import EvidenceStore
from trading_r1.runtime.session import recorder
from trading_r1.runtime.session.store import SessionStore
from trading_r1.runtime.sources import cite_source, source_scope
from trading_r1.tools.web.cache import FetchCache


class FilingRequest(BaseModel):
    section: int


class FilingOutput(Output):
    excerpt: Text
    price: int | None = None
    quote_date: str | None = None


async def filing(req: FilingRequest) -> Result:
    if req.section == -1:
        cite_source("https://example.test/quote", "Fixture quote", "A share was marked at 45.")
        return Result(
            FilingOutput(excerpt="Quote dated 2024-01-02", price=45, quote_date="2024-01-02")
        )
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
                grounding_review=True,
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
                        "path": "price",
                        "citations": [1],
                    }
                ],
                "eligible_actions": ["Hold"],
                "unresolved": ["NAV unknown; no sale authorized"],
                "next_calculation": "Mark the two shares",
            }
            plan = {"steps": [{"step": "Reconcile the position", "status": "in_progress"}]}
            if with_checkpoint:
                # Both container coercion and evidence resolution happen before the handler.
                plan["decision"] = json.dumps({**notes, "quote_date": "prev:e1.quote_date"})
            first = turn(
                ("price", "fixture_filing", {"section": -1}), ("plan", "update_plan", plan)
            )
            responses = [first]
            for i in range(5):
                responses.append(turn((f"section{i}", "fixture_filing", {"section": i})))
            done_plan = {"steps": [{"step": "Reconcile the position", "status": "completed"}]}
            if with_checkpoint:
                done_plan["decision"] = {**notes, "next_calculation": "Write report"}
            responses.append(
                turn(
                    ("mark", "calc", {"values": {"q": "prev:e1.price"}, "expression": "q*2"}),
                    ("plan_done", "update_plan", done_plan),
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
            cache = FetchCache()
            with recorder.bind(rec), source_scope(cache):
                result = await agent.run("Review this portfolio", on_event=rec)
            rec.end({**result, "citations": [asdict(c) for c in cache.citations]})
            # The final, tool-less grounding pass must see the original supporting excerpt.
            grounding = inputs[-1]
            cited = next(m for m in grounding if m.get("tool_call_id") == "price")
            self.assertIn("Quote dated 2024-01-02", cited["content"])
            self.assertIn("[1]", grounding[-1]["content"])
            return inputs[-2], result, evidence

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
            self.assertEqual(baseline[0], compact[0])
            notes_messages = [m for m in compact if "[Decision checkpoint:" in m["content"]]
            self.assertEqual([m["role"] for m in notes_messages], ["tool"])

            reopened = SessionStore(f"{temp}/on")
            rec = reopened.begin("fixture", "Continue", {})
            checkpoint = rec.load_checkpoint()
            self.assertIsNotNone(checkpoint)
            correction = {"role": "user", "content": "My mandate has changed; do not sell."}
            reopened_context = [
                baseline[0],
                {"role": "user", "content": "Earlier review"},
                correction,
            ]
            projected = checkpoint.project(reopened_context, current_run="new-run")
            self.assertEqual(projected[0], baseline[0])
            self.assertEqual(projected[-2]["role"], "assistant")
            self.assertIn("previous run", projected[-2]["content"])
            self.assertEqual(projected[-1], correction)
            self.assertEqual(checkpoint.notes.quote_date, "2024-01-02")
            self.assertEqual(checkpoint.notes.fx_date, "2024-01-01")
            self.assertEqual(checkpoint.notes.unresolved, ["NAV unknown; no sale authorized"])
            fact = checkpoint.notes.facts[0]
            restored = EvidenceStore.from_list(reopened.evidence("fixture"))
            self.assertEqual((fact.evidence_id, fact.path, fact.citations), ("e1", "price", [1]))
            self.assertEqual(restored.get(fact.evidence_id).payload["price"], 45)
            (source,) = reopened.sources("fixture")
            self.assertEqual((source["index"], source["record"]), (1, fact.evidence_id))
            self.assertFalse(
                any(m.get("type") == "decision.checkpoint" for m in reopened.lines("fixture"))
            )

    def test_uncheckpointed_round_stays_whole_until_refreshed_or_aged_out(self):
        notes = DecisionNotes(
            mandate="Hold unless thesis breaks",
            holdings=["HOLDCO: 2 shares"],
            facts=[],
            eligible_actions=["Hold"],
            unresolved=[],
            next_calculation="Check macro",
        )
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Review"},
            {"role": "assistant", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "round1 full", "_pointer": "[e1]"},
            {"role": "assistant", "tool_calls": [{"id": "c2"}]},
            {"role": "tool", "tool_call_id": "c2", "content": "round2 full", "_pointer": "[e2]"},
            {"role": "assistant", "tool_calls": [{"id": "c3"}]},
            {"role": "tool", "tool_call_id": "c3", "content": "round3 full", "_pointer": "[e3]"},
        ]
        # Committed in round 1: rounds 2 and 3 are un-checkpointed and both stay whole.
        stale_round = DecisionCheckpoint(notes=notes, run="r1", round=1, recent_evidence={})
        projected = stale_round.project(messages, current_run="r1")
        by_id = {m["tool_call_id"]: m["content"] for m in projected if m.get("role") == "tool"}
        self.assertEqual(by_id["c1"], "[e1]")
        self.assertEqual(by_id["c2"], "round2 full")
        self.assertIn("round3 full", by_id["c3"])

        # Refreshed in round 3: earlier rounds were just summarized and collapse immediately.
        refreshed = DecisionCheckpoint(notes=notes, run="r1", round=3, recent_evidence={})
        projected_now = refreshed.project(messages, current_run="r1")
        by_id_now = {
            m["tool_call_id"]: m["content"] for m in projected_now if m.get("role") == "tool"
        }
        self.assertEqual(by_id_now["c1"], "[e1]")
        self.assertEqual(by_id_now["c2"], "[e2]")
        self.assertIn("round3 full", by_id_now["c3"])

    def test_prior_run_checkpoint_does_not_compact_follow_up_run_tools(self):
        notes = DecisionNotes(
            mandate="Hold unless thesis breaks",
            holdings=["HOLDCO: 2 shares"],
            facts=[],
            eligible_actions=["Hold"],
            unresolved=[],
            next_calculation="Done",
        )
        prior = DecisionCheckpoint(notes=notes, run="old-run", round=1, recent_evidence={})
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Earlier review"},
            {"role": "assistant", "content": "Earlier answer."},
            {"role": "user", "content": "Compare banks vs telecoms"},
            {"role": "assistant", "tool_calls": [{"id": "n1"}]},
            {"role": "tool", "tool_call_id": "n1", "content": "fresh round1", "_pointer": "[e9]"},
            {"role": "assistant", "tool_calls": [{"id": "n2"}]},
            {"role": "tool", "tool_call_id": "n2", "content": "fresh round2", "_pointer": "[e10]"},
        ]
        projected = prior.project(messages, current_run="new-run")
        by_id = {m["tool_call_id"]: m["content"] for m in projected if m.get("role") == "tool"}
        self.assertEqual(by_id["n1"], "fresh round1")
        self.assertEqual(by_id["n2"], "fresh round2")
        self.assertNotIn("[Decision checkpoint:", by_id["n2"])
        self.assertIn("Earlier answer.", projected[2]["content"])
        self.assertIn("previous run", projected[2]["content"])
        self.assertEqual(projected[3], {"role": "user", "content": "Compare banks vs telecoms"})
