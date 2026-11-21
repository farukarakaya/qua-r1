# Copyright 2026 Tauric Research
# SPDX-License-Identifier: Apache-2.0

"""Filter and rank the full İş Yatırım BIST screening universe."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StringConstraints, model_validator

from trading_r1.contract.spec import Output, Provenance, Result, ToolError, ToolSpec, hint
from trading_r1.contract.units import Code, Count, Day, Multiple, Number, Percent, Text
from trading_r1.tools.data import bist

_FilterLabel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
_SortMetric = Literal[
    "market_cap_usd",
    "avg_volume_3m_usd",
    "pe",
    "pb",
    "ev_ebitda",
    "roe",
    "net_margin",
    "net_income_growth",
    "dividend_yield",
    "upside_potential",
]
_LOWEST_FIRST = {"pe", "pb", "ev_ebitda"}
_MAX_CAPTURES = 8
_CURSOR = re.compile(r"([0-9a-f]{32}):([1-9][0-9]{0,9})")
_STALE_CURSOR = "Unknown or expired BIST screen cursor. Start a new screen without a cursor."


class BistScreenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: _FilterLabel | None = Field(
        None, description="E.g. BIST100 or XIST:XU030. Omit for full BIST."
    )
    sector: _FilterLabel | None = Field(
        None, description="İş Yatırım name or code, e.g. Bankacılık or 0001."
    )
    recommendation: Literal["AL", "TUT", "SAT"] | None = None
    market_cap_usd_min: FiniteFloat | None = Field(None, ge=0, description="USD millions.")
    market_cap_usd_max: FiniteFloat | None = Field(None, ge=0, description="USD millions.")
    avg_volume_3m_usd_min: FiniteFloat | None = Field(None, ge=0, description="USD millions.")
    pe_min: FiniteFloat | None = Field(
        None, description="P/E multiple. Set a positive floor to exclude loss-makers."
    )
    pe_max: FiniteFloat | None = Field(None, description="P/E multiple.")
    pb_min: FiniteFloat | None = Field(None, description="P/B multiple.")
    pb_max: FiniteFloat | None = Field(None, description="P/B multiple.")
    ev_ebitda_min: FiniteFloat | None = Field(None, description="EV/EBITDA multiple.")
    ev_ebitda_max: FiniteFloat | None = Field(None, description="EV/EBITDA multiple.")
    roe_min: FiniteFloat | None = Field(
        None, description="Current ROE in percent, e.g. 20 for 20%."
    )
    net_margin_min: FiniteFloat | None = Field(None, description="Forecast net margin in percent.")
    net_income_growth_min: FiniteFloat | None = Field(
        None, description="Forecast net income growth in percent."
    )
    dividend_yield_min: FiniteFloat | None = Field(
        None, description="Reported-year dividend yield in percent."
    )
    upside_potential_min: FiniteFloat | None = Field(
        None, description="Analyst target upside in percent."
    )
    sort_by: _SortMetric = Field(
        "market_cap_usd",
        description=(
            "Positive multiples sort low to high, then nonpositive values. Other metrics "
            "sort high to low. Missing values last."
        ),
    )
    limit: int = Field(25, ge=1, le=50)
    cursor: str | None = Field(
        None, max_length=64, description="Use a non-null next_cursor from this turn on its own."
    )

    @model_validator(mode="after")
    def ordered_bounds(self):
        for metric in ("market_cap_usd", "pe", "pb", "ev_ebitda"):
            low, high = getattr(self, f"{metric}_min"), getattr(self, f"{metric}_max")
            if low is not None and high is not None and low > high:
                raise ValueError(f"{metric}_min cannot exceed {metric}_max")
        return self


class BistScreenRow(Output):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: Code
    name: Code
    sector: Code = None
    market_cap_usd: Number = Field(None, description="Market cap in USD millions.")
    avg_volume_3m_usd: Number = Field(None, description="3-month average volume in USD millions.")
    pe: Multiple = None
    pb: Multiple = None
    ev_ebitda: Multiple = None
    roe: Percent = None
    net_margin: Percent = Field(None, description="First forecast year net margin.")
    net_income_growth: Percent = Field(None, description="First forecast year net income growth.")
    dividend_yield: Percent = Field(None, description="Reported-year dividend yield.")
    upside_potential: Percent = None


class BistScreenOutput(Output):
    capture_id: Code
    captured_at: Day = Field(description="UTC capture time, not an upstream market data date.")
    total_matched: Count
    returned: Count = Field(description="Rows on this page.")
    returned_total: Count = Field(description="Distinct rows returned so far for this capture.")
    unseen: Count = Field(description="Matched rows not returned yet.")
    truncated: bool = Field(description="Whether matched rows remain unseen.")
    next_cursor: Code = None
    rows: Annotated[list[BistScreenRow], hint(rows=50)]
    unavailable_metrics: list[Code] = Field(
        default_factory=list, description="Display-only metrics whose upstream requests failed."
    )
    note: Text


@dataclass
class _Capture:
    id: str
    captured_at: str
    request: BistScreenRequest
    rows: list[BistScreenRow]
    unavailable_metrics: list[str]
    returned_total: int = 0


_captures: ContextVar[dict[str, _Capture] | None] = ContextVar("bist_screen_captures", default=None)


@contextmanager
def screen_capture_scope() -> Iterator[None]:
    """Keep a bounded set of screen captures for one research turn."""
    token = _captures.set({})
    try:
        yield
    finally:
        _captures.reset(token)


async def _capture(req: BistScreenRequest) -> _Capture:
    filters = req.model_dump(
        exclude={"index", "sector", "recommendation", "sort_by", "limit", "cursor"},
        exclude_none=True,
    )
    rows, unavailable = await bist.screen_stocks(
        index=req.index,
        sector=req.sector,
        recommendation=req.recommendation,
        required={req.sort_by} | {field.rsplit("_", 1)[0] for field in filters},
    )
    for field, bound in filters.items():
        metric, side = field.rsplit("_", 1)
        rows = [
            row
            for row in rows
            if (value := row.get(metric)) is not None
            and (value >= bound if side == "min" else value <= bound)
        ]
    multiple = req.sort_by in _LOWEST_FIRST

    def sort_key(row):
        value = row.get(req.sort_by)
        if value is None:
            return 2, 0, row["symbol"]
        if multiple and value <= 0:
            return 1, 0, row["symbol"]
        return 0, value if multiple else -value, row["symbol"]

    return _Capture(
        id=uuid4().hex,
        captured_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        request=req.model_copy(),
        rows=[BistScreenRow(**row) for row in sorted(rows, key=sort_key)],
        unavailable_metrics=list(unavailable),
    )


def _resume(req: BistScreenRequest, captures: dict[str, _Capture]) -> tuple[_Capture, int]:
    match = _CURSOR.fullmatch(req.cursor or "")
    if not match or (capture := captures.get(match[1])) is None:
        raise ToolError(_STALE_CURSOR)
    start = int(match[2])
    original = capture.request
    # Only page starts already issued by this capture are valid. No jumping ahead.
    if start > capture.returned_total or start >= len(capture.rows) or start % original.limit:
        raise ToolError(_STALE_CURSOR)
    if any(
        getattr(req, key) != getattr(original, key) for key in req.model_fields_set - {"cursor"}
    ):
        raise ToolError(
            "BIST screen filters, sort or limit changed. Pass only the cursor to continue."
        )
    return capture, start


async def _screen(req: BistScreenRequest) -> Result:
    captures = _captures.get()
    if req.cursor is not None:
        if captures is None:
            raise ToolError(_STALE_CURSOR)
        capture, start = _resume(req, captures)
    else:
        capture, start = await _capture(req), 0
        if captures is not None and len(capture.rows) > req.limit:
            if len(captures) >= _MAX_CAPTURES:
                del captures[next(iter(captures))]
            captures[capture.id] = capture

    original = capture.request
    rows = capture.rows[start : start + original.limit]
    capture.returned_total = max(capture.returned_total, start + len(rows))
    unseen = len(capture.rows) - capture.returned_total
    return Result(
        BistScreenOutput(
            capture_id=capture.id,
            captured_at=capture.captured_at,
            total_matched=len(capture.rows),
            returned=len(rows),
            returned_total=capture.returned_total,
            unseen=unseen,
            truncated=unseen > 0,
            next_cursor=(
                f"{capture.id}:{capture.returned_total}"
                if unseen and captures is not None
                else None
            ),
            rows=rows,
            unavailable_metrics=capture.unavailable_metrics,
            note=(
                "Market cap and 3-month average volume are in USD millions. Dividend yield is "
                "for the reported year. Net margin and net income growth are forecasts. "
                "Sector may be unavailable without a sector filter. Banks report under BRSA "
                "rules, not TMS 29; do not compare their raw multiples or ROE directly with "
                "inflation-adjusted companies."
            ),
        ),
        Provenance(
            source=bist.SCREEN_SOURCE, subject=original.index or "Full BIST", view=original.sort_by
        ),
    )


SPEC = ToolSpec(
    name="data_market_bist_screen",
    display_name="data.market.bist_screen",
    tier="deferred",
    result_kind="data",
    description=(
        "Screen the full BIST stock universe, including small- and mid-caps, by USD market cap "
        "and liquidity, valuation, profitability, dividends and analyst upside. Omit index to "
        "screen all stocks. Filters exclude missing measurements; omit forecast-only fields "
        "(net_margin, net_income_growth, upside_potential) when screening uncovered small-caps. "
        "In the same turn, pass a non-null next_cursor as cursor to continue the ranking, "
        "without changing filters, sort or limit. Unseen rows are not rejected companies. "
        "Verify leads with company filings and corporate actions before investing."
    ),
    request_schema=BistScreenRequest,
    handler=_screen,
    output=BistScreenOutput,
    tags=("BIST", "screening", "USD", "liquidity"),
)
