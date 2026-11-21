# Copyright 2026 Tauric Research
# SPDX-License-Identifier: Apache-2.0

"""Source-linked discovery decisions and coverage in the existing session evidence log."""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Iterable
from typing import Annotated, Any, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from trading_r1.contract.spec import Output, Provenance, Result, ToolError, ToolSpec, hint
from trading_r1.contract.units import Code, Count, Day, Exact, Price, Text
from trading_r1.runtime.chaining import ChainFailure, resolve_one
from trading_r1.runtime.evidence import EvidenceRecord, EvidenceStore, lineage
from trading_r1.tools.data.bist import bist_ticker
from trading_r1.tools.data.common import Symbol
from trading_r1.tools.data.market.bist_screen import SPEC as BIST_SCREEN
from trading_r1.tools.data.market.quotes import SPEC as QUOTES

NAME = "core_record_discovery"
_Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=360)]
_EvidenceID = Annotated[str, Field(pattern=r"^e[0-9]{1,16}$")]
_Track = Literal["compounder", "cyclical_turn", "supply_chain"]
_Verdict = Literal["watch", "pass", "eligible"]
_US_TICKER = re.compile(r"[A-Z][A-Z0-9]*(?:\.[A-Z0-9]+)?")
_PUBLICATION = {
    "data_equity_filings": re.compile(r"filings\[[0-9]+\]\.filed"),
    "data_market_istfin_disclosure": re.compile(r"disclosure\.published_at"),
    "data_market_istfin_news": re.compile(r"items\[[0-9]+\]\.published_at"),
    "data_market_news": re.compile(r"items\[[0-9]+\]\.published"),
}
_NOT_COMPANY_EVIDENCE = {NAME, BIST_SCREEN.name, "search"}


class EvidenceRef(Output):
    evidence_id: _EvidenceID
    path: _Short = Field(description="Payload field such as rows[0].text. Not a prev: expression.")


class PriceHurdle(Output):
    quote_evidence: _EvidenceID = Field(description="A data.market.quotes result for this symbol.")
    calculation: EvidenceRef = Field(description="Numeric result from calc or a valuation tool.")
    assumptions: _Short = Field(description="Method, units, horizon and fixed assumptions.")
    plausible_range: _Short = Field(description="Plausible range for comparison and why.")


class Decision(Output):
    symbol: Symbol
    track: _Track
    verdict: _Verdict = Field(description="Eligible means compare in portfolio review, not buy.")
    change: _Short = Field(description="What changed versus the prior baseline?")
    business_impact: _Short = Field(description="How does the change reach revenue or cash flow?")
    counterevidence: _Short = Field(description="Strongest contrary fact or reason to pass.")
    next_milestone: _Short = Field(description="Dated event or observable test of this thesis.")
    evidence: Annotated[list[EvidenceRef], Field(min_length=1, max_length=8)]
    publication: EvidenceRef | None = Field(
        None, description="Recorded public filing or release date field. Omit if unknown."
    )
    early_claim: bool = Field(
        False, description="Claim the recorded signal predates the milestone."
    )
    price_hurdle: PriceHurdle | None = Field(
        None, description="Price-implied operating hurdle. Omit if not established."
    )


class DiscoveryRequest(Output):
    capture_id: str | None = Field(None, pattern=r"^[0-9a-f]{32}$", description="BIST capture ID.")
    us_universe: Annotated[list[Symbol], Field(min_length=1, max_length=100)] | None = Field(
        None, description="Explicit US ticker list. Do not substitute a different universe."
    )
    decisions: list[Decision] = Field(
        default_factory=list, max_length=12, description="Omit for coverage without new verdicts."
    )

    @model_validator(mode="after")
    def one_universe(self) -> Self:
        if (self.capture_id is None) == (self.us_universe is None):
            raise ValueError("Supply exactly one of capture_id or us_universe.")
        return self


class Valuation(Output):
    price: Price
    currency: Code
    quote_date: Day
    implied: Exact


class SavedDecision(Decision):
    published_at: Day = None
    valuation: Valuation | None = None


class DiscoveryOutput(Output):
    market: Code
    capture_id: Code = None
    captured_at: Day = None
    us_universe: list[Code] = []
    matched: Count
    returned: Count
    unseen: Count
    examined: Count
    rejected: Count
    watch: Count
    eligible: Count
    decisions: Annotated[list[SavedDecision], hint(hidden=True)]
    verdicts: dict[str, Code]
    note: Text = None


def _value(record: EvidenceRecord, ref: EvidenceRef) -> Any:
    try:
        value = resolve_one(f"prev:{ref.evidence_id}.{ref.path}", {record.id: record.payload})
    except ValueError as err:
        raise ToolError(f"Discovery evidence has an invalid path in {ref.evidence_id}.") from err
    if isinstance(value, ChainFailure):
        raise ToolError(f"Discovery evidence has a missing or null value in {ref.evidence_id}.")
    if isinstance(value, str | dict | list) and not value:
        raise ToolError(f"Discovery evidence {ref.evidence_id}.{ref.path} is empty.")
    return value


def _date(value: object, now: dt.datetime) -> bool:
    if not isinstance(value, str):
        return False
    try:
        if len(value) == 10:
            return dt.date.fromisoformat(value) <= now.date()
        stamp = dt.datetime.fromisoformat(value)
        return stamp.tzinfo is not None and stamp <= now
    except (OverflowError, ValueError):
        return False


def _publication(ref: EvidenceRef, store: EvidenceStore, now: dt.datetime) -> str:
    record = store.get(ref.evidence_id)
    if not record or not (rule := _PUBLICATION.get(record.tool)) or not rule.fullmatch(ref.path):
        raise ToolError("Use a recorded publication date field, not a report or retrieval date.")
    if record.tool == "data_market_news":
        basis = ref.model_copy(update={"path": ref.path.rsplit(".", 1)[0] + ".time_basis"})
        if _value(record, basis) != "published":
            raise ToolError("First-seen news time is not a publication time.")
    published = _value(record, ref)
    if not _date(published, now):
        raise ToolError("Publication date is missing, invalid or in the future.")
    return published


def _valuation(
    symbol: str, hurdle: PriceHurdle, store: EvidenceStore, now: dt.datetime
) -> Valuation:
    record = store.get(hurdle.quote_evidence)
    if not record or record.tool != QUOTES.name:
        raise ToolError("Price hurdle needs a recorded quote for the candidate.")
    quote = record.payload.get("quotes", {}).get(symbol, {})
    price, currency = quote.get("price"), quote.get("currency")
    date = quote.get("as_of") or quote.get("session_date")
    if (
        isinstance(price, bool)
        or not isinstance(price, int | float)
        or not math.isfinite(price)
        or price <= 0
        or not currency
        or not _date(date, now)
    ):
        raise ToolError(f"Price hurdle needs a dated, positive quote and currency for {symbol}.")
    ref = hurdle.calculation
    calc = store.get(ref.evidence_id)
    if not calc or (calc.tool != "calc" and not calc.tool.startswith("compute_valuation_")):
        raise ToolError("Price hurdle must point to a calculation result.")
    if not any(node.id == record.id for node in lineage(calc.id, store.all()).nodes):
        raise ToolError("Chain the quote into the price hurdle calculation using prev:.")
    implied = _value(calc, ref)
    if (
        isinstance(implied, bool)
        or not isinstance(implied, int | float)
        or not math.isfinite(implied)
    ):
        raise ToolError("Price hurdle must point to a finite number.")
    return Valuation(price=price, currency=currency, quote_date=date, implied=implied)


def _bist_coverage(records: Iterable[EvidenceRecord], capture_id: str) -> tuple[int, set[str], str]:
    pages = [
        r.payload
        for r in records
        if r.tool == BIST_SCREEN.name and r.payload.get("capture_id") == capture_id
    ]
    if not pages:
        raise ToolError("No such BIST screen capture in this session. Run a screen first.")
    returned = {row["symbol"] for page in pages for row in page["rows"]}
    return int(pages[0]["total_matched"]), returned, pages[0]["captured_at"]


def _save(decision: Decision, symbol: str, store: EvidenceStore, now: dt.datetime) -> SavedDecision:
    for ref in decision.evidence:
        record = store.get(ref.evidence_id)
        if record is None:
            raise ToolError(f"No discovery evidence {ref.evidence_id} in this session.")
        if record.tool in _NOT_COMPANY_EVIDENCE:
            raise ToolError(
                "Use company or industry evidence, not a screen, search or prior verdict."
            )
        _value(record, ref)
    published = _publication(decision.publication, store, now) if decision.publication else None
    if decision.early_claim and published is None:
        raise ToolError("An early discovery claim needs a recorded publication date.")
    valuation = (
        _valuation(symbol, decision.price_hurdle, store, now) if decision.price_hurdle else None
    )
    if decision.verdict == "eligible" and (published is None or valuation is None):
        raise ToolError(
            "Eligibility needs a publication date and price hurdle. Use watch if unknown."
        )
    return SavedDecision(
        **(decision.model_dump() | {"symbol": symbol}), published_at=published, valuation=valuation
    )


def build_discovery_spec(store: EvidenceStore) -> ToolSpec:
    async def handler(req: DiscoveryRequest) -> Result:
        records = store.all()
        universe = sorted(req.us_universe or [])
        capture_id = req.capture_id
        if capture_id:
            matched, returned, captured_at = _bist_coverage(records, capture_id)
        else:
            if len(set(universe)) != len(universe) or any(
                not _US_TICKER.fullmatch(s) or bist_ticker(s) for s in universe
            ):
                raise ToolError("Use a distinct list of plain US equity tickers such as BRK.B.")
            matched, returned, captured_at = len(universe), set(universe), None

        latest = {
            decision["symbol"]: decision["verdict"]
            for r in records
            if r.tool == NAME
            and r.payload.get("capture_id") == capture_id
            and r.payload.get("us_universe") == universe
            for decision in r.payload.get("decisions", [])
        }
        saved = []
        seen = set()
        now = dt.datetime.now(dt.UTC)
        for decision in req.decisions:
            symbol = decision.symbol
            if capture_id and (ticker := bist_ticker(symbol)):
                symbol = f"XIST:{ticker}"
            if symbol not in returned:
                raise ToolError(
                    f"{symbol} was not returned by this capture or on the supplied list."
                )
            if symbol in seen:
                raise ToolError(f"Submit only one verdict for {symbol} per call.")
            seen.add(symbol)
            saved.append(_save(decision, symbol, store, now))
            latest[symbol] = decision.verdict

        verdicts = list(latest.values())
        return Result(
            DiscoveryOutput(
                market="BIST" if capture_id else "US",
                capture_id=capture_id,
                captured_at=captured_at,
                us_universe=universe,
                matched=matched,
                returned=len(returned),
                unseen=matched - len(returned),
                examined=len(latest),
                rejected=verdicts.count("pass"),
                watch=verdicts.count("watch"),
                eligible=verdicts.count("eligible"),
                decisions=saved,
                verdicts=latest,
                note=None if capture_id else "US coverage is limited to the supplied list.",
            ),
            Provenance(method="Discovery judgments, not verified market facts"),
        )

    return ToolSpec(
        name=NAME,
        display_name="core.record_discovery",
        tier="deferred",
        result_kind="control",
        evidence=True,
        description=(
            "Save source-linked discovery verdicts for a BIST screen capture or a supplied US "
            "equity list in this session. Omit decisions to check coverage. Record watch, pass "
            "or eligible for portfolio review. Eligibility needs a dated publication and a "
            "calculated price hurdle. Do not record trade sizes or recommendations."
        ),
        request_schema=DiscoveryRequest,
        handler=handler,
        output=DiscoveryOutput,
        tags=("core", "discovery", "research"),
    )
