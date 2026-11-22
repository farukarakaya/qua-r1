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
from trading_r1.tools.core.evidence import build_evidence_spec
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
            self.assertNotIn('"call_id"', notes_messages[0]["content"])

            reopened = SessionStore(f"{temp}/on")
            rec = reopened.begin("fixture", "Continue", {})
            checkpoint = rec.load_checkpoint()
            self.assertIsNotNone(checkpoint)
            self.assertEqual(checkpoint.call_id, "plan_done")
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

    def test_post_checkpoint_rounds_stay_whole_with_stable_prefix_until_refreshed(self):
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
            {"role": "assistant", "tool_calls": [{"id": "c1"}, {"id": "p1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "round1 full", "_pointer": "[e1]"},
            {"role": "tool", "tool_call_id": "p1", "content": "steps=1 | completed=0"},
            {"role": "assistant", "tool_calls": [{"id": "c2"}]},
            {
                "role": "tool",
                "tool_call_id": "c2",
                "content": "read1 full",
                "_pointer": "[Evidence read: e1]",
            },
            {"role": "assistant", "tool_calls": [{"id": "c3"}]},
            {
                "role": "tool",
                "tool_call_id": "c3",
                "content": "read2 full",
                "_pointer": "[Evidence read: e2]",
            },
            {"role": "assistant", "tool_calls": [{"id": "c4"}, {"id": "p4"}]},
            {
                "role": "tool",
                "tool_call_id": "c4",
                "content": "read3 full",
                "_pointer": "[Evidence read: e3]",
            },
            {"role": "tool", "tool_call_id": "p4", "content": "steps=1 | completed=1"},
        ]
        # Committed at p1: three later rounds stay whole and the note stays anchored on p1.
        committed = DecisionCheckpoint(
            notes=notes, run="r1", round=1, call_id="p1", recent_evidence={}
        )
        projected_3 = committed.project(messages[:9], current_run="r1")
        projected_4 = committed.project(messages, current_run="r1")
        self.assertEqual(projected_4[: len(projected_3)], projected_3)
        by_id = {m["tool_call_id"]: m["content"] for m in projected_4 if m.get("role") == "tool"}
        self.assertEqual(by_id["c1"], "round1 full")
        self.assertEqual(by_id["c2"], "read1 full")
        self.assertEqual(by_id["c3"], "read2 full")
        self.assertEqual(by_id["c4"], "read3 full")
        self.assertIn("[Decision checkpoint:", by_id["p1"])
        self.assertNotIn("[Decision checkpoint:", by_id["p4"])

        # Legacy checkpoints without call_id fall back to the commit round.
        legacy = DecisionCheckpoint(notes=notes, run="r1", round=3, recent_evidence={})
        by_id_legacy = {
            m["tool_call_id"]: m["content"]
            for m in legacy.project(messages, current_run="r1")
            if m.get("role") == "tool"
        }
        self.assertEqual(by_id_legacy["c1"], "[e1]")
        self.assertEqual(by_id_legacy["c2"], "[Evidence read: e1]")
        self.assertEqual(by_id_legacy["c4"], "read3 full")
        self.assertIn("[Decision checkpoint:", by_id_legacy["c3"])

        # Refreshed at p4: earlier rounds collapse and the note moves onto p4.
        refreshed = DecisionCheckpoint(
            notes=notes, run="r1", round=4, call_id="p4", recent_evidence={}
        )
        by_id_now = {
            m["tool_call_id"]: m["content"]
            for m in refreshed.project(messages, current_run="r1")
            if m.get("role") == "tool"
        }
        self.assertEqual(by_id_now["c1"], "[e1]")
        self.assertEqual(by_id_now["c2"], "[Evidence read: e1]")
        self.assertEqual(by_id_now["c3"], "[Evidence read: e2]")
        self.assertEqual(by_id_now["c4"], "read3 full")
        self.assertNotIn("[Decision checkpoint:", by_id_now["p1"])
        self.assertIn("[Decision checkpoint:", by_id_now["p4"])

    def test_prior_run_checkpoint_collapses_prior_turns_and_keeps_follow_up_whole(self):
        notes = DecisionNotes(
            mandate="Hold unless thesis breaks",
            holdings=["HOLDCO: 2 shares"],
            facts=[],
            eligible_actions=["Hold"],
            unresolved=[],
            next_calculation="Done",
        )
        prior = DecisionCheckpoint(
            notes=notes, run="old-run", round=1, call_id="oldp", recent_evidence={}
        )
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Earlier review"},
            {"role": "assistant", "tool_calls": [{"id": "old1"}]},
            {"role": "tool", "tool_call_id": "old1", "content": "prior full", "_pointer": "[e1]"},
            {"role": "assistant", "content": "Earlier answer."},
            {"role": "user", "content": "Compare banks vs telecoms"},
            {"role": "assistant", "tool_calls": [{"id": "n1"}]},
            {"role": "tool", "tool_call_id": "n1", "content": "fresh round1", "_pointer": "[e9]"},
            {"role": "assistant", "tool_calls": [{"id": "n2"}]},
            {"role": "tool", "tool_call_id": "n2", "content": "fresh round2", "_pointer": "[e10]"},
            {"role": "assistant", "tool_calls": [{"id": "n3"}]},
            {"role": "tool", "tool_call_id": "n3", "content": "fresh round3", "_pointer": "[e11]"},
        ]
        projected = prior.project(messages, current_run="new-run")
        by_id = {m["tool_call_id"]: m["content"] for m in projected if m.get("role") == "tool"}
        self.assertEqual(by_id["old1"], "[e1]")
        self.assertEqual(by_id["n1"], "fresh round1")
        self.assertEqual(by_id["n2"], "fresh round2")
        self.assertEqual(by_id["n3"], "fresh round3")
        self.assertNotIn("[Decision checkpoint:", by_id["n3"])
        self.assertIn("Earlier answer.", projected[4]["content"])
        self.assertIn("previous run", projected[4]["content"])
        self.assertEqual(projected[5], {"role": "user", "content": "Compare banks vs telecoms"})

    async def test_multi_round_evidence_reads_stay_whole_after_checkpoint_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SessionStore(temp)
            rec = store.begin("fixture", "Review this portfolio", {})
            evidence = EvidenceStore()
            evidence.observe(rec.record_evidence)
            registry = catalog_tool_registry()
            registry.register(build_evidence_spec(evidence))
            registry.register(
                ToolSpec(
                    name="fixture_filing",
                    description="Read a synthetic filing section.",
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
                "facts": [],
                "eligible_actions": ["Hold"],
                "unresolved": [],
                "next_calculation": "Read evidence and answer",
            }
            responses = [
                turn(
                    ("f1", "fixture_filing", {"section": -1}),
                    (
                        "p1",
                        "update_plan",
                        {
                            "steps": [{"step": "Read filing", "status": "completed"}],
                            "decision": notes,
                        },
                    ),
                ),
                turn(("ev1", "evidence", {"id": "e1", "path": "price"})),
                turn(("ev2", "evidence", {"id": "e1", "path": "quote_date"})),
                turn(("ev3", "evidence", {"id": "e1"})),
            ]
            inputs = []

            async def generate(messages, **_):
                inputs.append(json.loads(json.dumps(messages)))
                if responses:
                    return responses.pop(0)
                return AssistantTurn(content="Quote is 45.", finish_reason="stop")

            agent.provider.generate = AsyncMock(side_effect=generate)
            saves = 0
            orig_save = rec.save_checkpoint

            def counted_save(ckpt):
                nonlocal saves
                saves += 1
                orig_save(ckpt)

            rec.save_checkpoint = counted_save
            with recorder.bind(rec):
                result = await agent.run("Review this portfolio", on_event=rec)

            self.assertEqual(result["messages"][-1]["content"], "Quote is 45.")
            self.assertEqual(saves, 1)
            final_tools = {
                m["tool_call_id"]: m["content"] for m in inputs[-1] if m.get("role") == "tool"
            }
            self.assertIn("[Decision checkpoint:", final_tools["p1"])
            self.assertNotIn("[Evidence read:", final_tools["ev1"])
            self.assertNotIn("[Evidence read:", final_tools["ev2"])
            self.assertNotIn("[Evidence read:", final_tools["ev3"])
            self.assertIn("45", final_tools["ev1"])
            self.assertIn("2024-01-02", final_tools["ev2"])
            self.assertIn("Quote dated 2024-01-02", final_tools["ev3"])
            # Once committed on round 1, rounds 2..5 only append to an unchanged prefix.
            for idx in range(1, len(inputs) - 1):
                self.assertEqual(inputs[idx], inputs[idx + 1][: len(inputs[idx])])
