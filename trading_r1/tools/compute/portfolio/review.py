# Copyright 2026 Tauric Research
# SPDX-License-Identifier: Apache-2.0

"""Compare explicitly funded, quote-date trade lists without authorizing conditional sales."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trading_r1.contract.spec import Output, Provenance, Result, ToolError, ToolSpec
from trading_r1.contract.units import Code, Fraction, Money


class SaleBasis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Literal["thesis_break", "mandate_change", "review_only", "payout_cut_only"]
    original_thesis: str = ""
    contrary_filing: str = ""


class ReviewRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    holdings: dict[str, float] = Field(description="Current marked values in one currency.")
    opening_cash: float = Field(ge=0, description="Cash available now. Exclude unpaid dividends.")
    external_cash: float = Field(default=0, ge=0, description="Explicitly committed cash only.")
    commission_rate: float = Field(ge=0, lt=1, description="Per-side commission as a fraction.")
    spread_rate: float = Field(ge=0, lt=1, description="Per-side slippage as a fraction of mid.")
    stage_trades: dict[str, float] = Field(description="Funded first-stage notionals (+buy/-sell).")
    full_trades: dict[str, float] = Field(description="Full target notionals (+buy/-sell).")
    mandate_sales_authorized: bool = False
    sale_basis: dict[str, SaleBasis] = Field(default_factory=dict)


class Scenario(Output):
    status: Code
    external_cash: Money
    buys: Money
    sells: Money
    fees: Money
    spreads: Money
    closing_cash: Money
    turnover_one_way: Fraction
    blocked_sales: list[Code]


class ReviewOutput(Output):
    hold: Scenario
    first_stage: Scenario
    full_target: Scenario


def compare_review(req: ReviewRequest) -> ReviewOutput:
    opening_nav = math.fsum(req.holdings.values()) + req.opening_cash
    if any(v < 0 for v in req.holdings.values()) or opening_nav <= 0:
        raise ToolError("Use long-only holdings and positive opening NAV.")

    def allowed(name: str) -> bool:
        basis = req.sale_basis.get(name)
        if basis is None:
            return False
        if basis.reason == "mandate_change":
            return req.mandate_sales_authorized
        return bool(
            basis.reason == "thesis_break"
            and basis.original_thesis.strip()
            and basis.contrary_filing.strip()
        )

    def scenario(trades: dict[str, float], external: float) -> Scenario:
        if any(-v > req.holdings.get(name, 0) for name, v in trades.items() if v < 0):
            raise ToolError("A sell exceeds its marked holding value.")
        buys = math.fsum(v for v in trades.values() if v > 0)
        sells = math.fsum(-v for v in trades.values() if v < 0)
        gross = buys + sells
        fees, spreads = gross * req.commission_rate, gross * req.spread_rate
        cash = math.fsum([req.opening_cash, external, sells, -buys, -fees, -spreads])
        blocked = sorted(name for name, v in trades.items() if v < 0 and not allowed(name))
        return Scenario(
            status="unfunded" if cash < -1e-8 else "conditional" if blocked else "funded",
            external_cash=external,
            buys=buys,
            sells=sells,
            fees=fees,
            spreads=spreads,
            closing_cash=cash,
            turnover_one_way=gross / (2 * opening_nav),
            blocked_sales=blocked,
        )

    return ReviewOutput(
        hold=scenario({}, 0),
        first_stage=scenario(req.stage_trades, req.external_cash),
        full_target=scenario(req.full_trades, req.external_cash),
    )


async def _handler(req: ReviewRequest) -> Result:
    return Result(compare_review(req), Provenance(source="Portfolio cash ledger"))


SPEC = ToolSpec(
    name="compute_portfolio_review",
    display_name="compute.portfolio.review",
    result_kind="compute",
    tier="deferred",
    description=(
        "Compare hold, first stage and full target using explicitly funded trades at the same "
        "marks. Draft trades only. Sells require an authorized mandate change or a documented "
        "thesis break. Other sales are conditional. Negative cash is unfunded. Turnover is "
        "half gross trade value divided by opening holdings plus opening cash."
    ),
    request_schema=ReviewRequest,
    handler=_handler,
    output=ReviewOutput,
    tags=("compute", "portfolio", "trades"),
)
