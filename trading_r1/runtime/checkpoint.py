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
    call_id: str = ""

    @classmethod
    def commit(
        cls,
        notes: DecisionNotes,
        store: EvidenceStore,
        *,
        run: str,
        round_: int,
        call_id: str = "",
    ) -> DecisionCheckpoint:
        return cls(
            notes=notes,
            run=run,
            round=round_,
            call_id=call_id,
            recent_evidence={
                r.id: f"{r.provenance.get('as_of') or 'date not supplied'} | {r.call[:200]}"[:320]
                for r in store.all()[-24:]
            },
        )

    def project(self, messages: list[dict[str, Any]], *, current_run: str) -> list[dict[str, Any]]:
        """Replace pre-checkpoint evidence outputs with pointers; keep native call/result pairs."""
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
            + json.dumps(
                self.model_dump(exclude={"call_id"}), ensure_ascii=False, separators=(",", ":")
            )
        )
        if prior:
            # Collapse tool pointers from completed earlier turns before the latest user prompt,
            # while leaving everything in the new turn whole until it commits its own checkpoint.
            last_user = next(
                (i for i in reversed(range(len(history))) if history[i].get("role") == "user"),
                None,
            )
            projected = [
                {**m, "content": m["_pointer"]}
                if last_user is not None and i < last_user and m.get("_pointer")
                else m
                for i, m in enumerate(history)
            ]
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
        # Anchor collapse and the note to the commit round so later reads stay whole and the
        # prefix stays stable until the next checkpoint update.
        note_idx: int | None = None
        cutoff = 0
        if self.call_id:
            note_idx = next(
                (
                    i
                    for i in reversed(range(len(history)))
                    if history[i].get("role") == "tool"
                    and history[i].get("tool_call_id") == self.call_id
                ),
                None,
            )
            if note_idx is not None:
                cutoff = max((i for i in tool_rounds if i < note_idx), default=note_idx)
        elif 1 <= self.round <= len(tool_rounds):
            cutoff = tool_rounds[self.round - 1]
            next_round = tool_rounds[self.round] if self.round < len(tool_rounds) else len(history)
            note_idx = next(
                (
                    i
                    for i in reversed(range(cutoff, next_round))
                    if history[i].get("role") == "tool"
                ),
                None,
            )
        # Carry working notes as data, never as system or user instructions. Keep the
        # provider's native assistant tool calls paired with their original results.
        projected = [system]
        for i, message in enumerate(history):
            if i < cutoff and message.get("_pointer"):
                message = {**message, "content": message["_pointer"]}
            if i == note_idx:
                message = {**message, "content": message["content"] + note}
            projected.append(message)
        return projected
