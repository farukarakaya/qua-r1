# Copyright 2026 Tauric Research
# SPDX-License-Identifier: Apache-2.0

"""A small, source-linked working view, separate from the canonical transcript."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from trading_r1.runtime.evidence import EvidenceStore

Short = Annotated[str, Field(max_length=320)]
EvidenceID = Annotated[str, Field(pattern=r"^e\d+$")]


class DecisionFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact: Short
    evidence_id: EvidenceID
    path: Short = ""
    citations: list[int] = Field(default_factory=list, max_length=8)
    status: Literal["verified", "paid", "approved", "proposed", "unknown"] = "verified"


class DecisionNotes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mandate: Short
    holdings: list[Short] = Field(max_length=32)
    quote_date: Short = ""
    fx_date: Short = ""
    facts: list[DecisionFact] = Field(max_length=40)
    eligible_actions: list[Short] = Field(max_length=12)
    unresolved: list[Short] = Field(max_length=12)
    next_calculation: Short


class DecisionCheckpoint(BaseModel):
    notes: DecisionNotes
    run: str
    round: int
    recent_evidence: dict[EvidenceID, Short]

    @classmethod
    def commit(
        cls, notes: DecisionNotes, store: EvidenceStore, *, run: str, round_: int
    ) -> DecisionCheckpoint:
        return cls(
            notes=notes,
            run=run,
            round=round_,
            recent_evidence={
                r.id: f"{r.provenance.get('as_of') or 'date not supplied'} | {r.call[:200]}"[:320]
                for r in store.all()[-24:]
            },
        )

    def project(self, messages: list[dict[str, Any]], *, current_run: str) -> list[dict[str, Any]]:
        """Replace older evidence outputs with pointers; keep every native call/result pair."""
        system, *history = messages
        prior = self.run != current_run
        note = (
            "\n\n[Decision checkpoint: research notes, not user instructions. "
            "Confirm quote and FX dates before sizing; preserve unknowns. "
            "Retrieve exact values by evidence id and path, not by repeating research."
            + (
                " This checkpoint is from a previous run; do not assume its mandate still applies."
                if prior
                else ""
            )
            + "]\n"
            + json.dumps(self.model_dump(), ensure_ascii=False, separators=(",", ":"))
        )
        if prior:
            # A prior-run checkpoint is read-only context for the new turn; it never compacts
            # fresh tool outputs in a run that has not committed its own checkpoint.
            projected = list(history)
            last_user = next(
                (i for i in reversed(range(len(projected))) if projected[i].get("role") == "user"),
                None,
            )
            if last_user is not None and last_user > 0:
                prev = projected[last_user - 1]
                if (
                    prev.get("role") == "assistant"
                    and not prev.get("tool_calls")
                    and isinstance(prev.get("content"), str)
                ):
                    projected[last_user - 1] = {**prev, "content": prev["content"] + note}
                else:
                    projected.insert(last_user, {"role": "assistant", "content": note.lstrip()})
            return [system, *projected]

        tool_rounds = [i for i, m in enumerate(history) if m.get("tool_calls")]
        # Collapse through the prior round only when decision notes were refreshed this round;
        # otherwise keep two recent rounds whole so un-checkpointed research survives follow-ups.
        keep = 1 if self.round >= len(tool_rounds) else 2
        cutoff = tool_rounds[-keep] if len(tool_rounds) >= keep else 0
        # Carry working notes as data, never as system or user instructions. Keep the
        # provider's native assistant tool calls paired with their original results.
        last_result = next(
            (i for i in reversed(range(len(history))) if history[i].get("role") == "tool"), None
        )
        projected = [system]
        for i, message in enumerate(history):
            if i < cutoff and message.get("_pointer"):
                message = {**message, "content": message["_pointer"]}
            if i == last_result:
                message = {**message, "content": message["content"] + note}
            projected.append(message)
        return projected
