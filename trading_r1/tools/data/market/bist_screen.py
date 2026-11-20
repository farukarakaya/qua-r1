# Copyright 2026 Tauric Research
# SPDX-License-Identifier: Apache-2.0

"""Filter and rank the full İş Yatırım BIST screening universe."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StringConstraints, model_validator

from trading_r1.contract.spec import Output, Provenance, Result, ToolSpec, hint
from trading_r1.contract.units import Code, Count, Multiple, Number, Percent, Text
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

    @model_validator(mode="after")
    def ordered_bounds(self):
        for metric in ("market_cap_usd", "pe", "pb", "ev_ebitda"):
            low, high = getattr(self, f"{metric}_min"), getattr(self, f"{metric}_max")
            if low is not None and high is not None and low > high:
                raise ValueError(f"{metric}_min cannot exceed {metric}_max")
        return self


class BistScreenRow(Output):
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
    total_matched: Count
    truncated: bool
    rows: Annotated[list[BistScreenRow], hint(rows=50)]
    unavailable_metrics: list[Code] = Field(
        default_factory=list, description="Display-only metrics whose upstream requests failed."
    )
    note: Text


async def _screen(req: BistScreenRequest) -> Result:
    filters = req.model_dump(
        exclude={"index", "sector", "recommendation", "sort_by", "limit"}, exclude_none=True
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

    rows.sort(key=sort_key)
    return Result(
        BistScreenOutput(
            total_matched=len(rows),
            truncated=len(rows) > req.limit,
            rows=[BistScreenRow(**row) for row in rows[: req.limit]],
            unavailable_metrics=unavailable,
            note=(
                "Market cap and 3-month average volume are in USD millions. Dividend yield is "
                "for the reported year. Net margin and net income growth are forecasts. "
                "Sector may be unavailable without a sector filter. Banks report under BRSA "
                "rules, not TMS 29; do not compare their raw multiples or ROE directly with "
                "inflation-adjusted companies."
            ),
        ),
        Provenance(source=bist.SCREEN_SOURCE, subject=req.index or "Full BIST", view=req.sort_by),
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
        "Sort then limit the results. Verify leads with company filings and corporate actions "
        "before investing."
    ),
    request_schema=BistScreenRequest,
    handler=_screen,
    output=BistScreenOutput,
    tags=("BIST", "screening", "USD", "liquidity"),
)
