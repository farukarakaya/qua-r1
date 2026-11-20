# Copyright 2026 Tauric Research
# SPDX-License-Identifier: Apache-2.0

"""A small, source-linked working view, separate from the canonical transcript."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from trading_r1.runtime.evidence import EvidenceStore

Short = Annotated[str, Field(max_length=320)]
EvidenceID = Annotated[str, Field(pattern=r"^e\d+$")]


class DecisionFact(BaseModel):
    fact: Short
    evidence_id: EvidenceID
    path: Short = ""
    citations: list[int] = Field(default_factory=list, max_length=8)
    status: Literal["verified", "paid", "approved", "proposed", "unknown"] = "verified"


class DecisionNotes(BaseModel):
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
                r.id: str(r.provenance.get("as_of") or "date not supplied") + " | " + r.call[:200]
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
        tool_rounds = [i for i, m in enumerate(history) if m.get("tool_calls")]
        cutoff = tool_rounds[-1] if tool_rounds else 0
        projected = [{**system, "content": system["content"] + note}]
        for i, message in enumerate(history):
            if i < cutoff and message.get("_pointer"):
                message = {**message, "content": message["_pointer"]}
            projected.append(message)
        return projected
