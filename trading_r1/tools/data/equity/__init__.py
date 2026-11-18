# Copyright 2026 Tauric Research
# SPDX-License-Identifier: Apache-2.0

"""Equity data tools (Tauric): reference, fundamentals, ratios, comps, estimates,
earnings, corporate actions, ownership, SEC filings, digest and thesis."""

from dataclasses import replace

from pydantic import BaseModel

from trading_r1.contract.spec import Result, ToolError, ToolSpec
from trading_r1.tools.data.bist import bist_ticker
from trading_r1.tools.data.equity import (
    comps,
    corporate_actions,
    digest,
    earnings,
    estimates,
    filings,
    fundamentals,
    ownership,
    ratios,
    reference,
)


def _non_bist(spec: ToolSpec, alternative: str) -> ToolSpec:
    async def handler(req: BaseModel) -> Result:
        symbols = (req.symbol, *(getattr(req, "peers", None) or ()))
        if any(bist_ticker(symbol) for symbol in symbols):
            raise ToolError(f"This tool does not serve BIST. {alternative}")
        return await spec.handler(req)

    return replace(spec, handler=handler, description=f"{spec.description} For BIST: {alternative}")


EQUITY_DATA_SPECS = [
    _non_bist(reference.SPEC, "Use data.market.bist with dataset=profile."),
    _non_bist(fundamentals.SPEC, "Use data.market.bist for profile and financial statements."),
    _non_bist(ratios.SPEC, "Use data.market.bist for native financial statements and profile."),
    _non_bist(comps.SPEC, "Use data.market.bist with dataset=screener for BIST peers."),
    _non_bist(
        estimates.SPEC, "EPS estimates are unavailable; see data.market.bist analyst_targets."
    ),
    _non_bist(earnings.SPEC, "Use data.market.bist filing_calendar for expected filing windows."),
    _non_bist(corporate_actions.SPEC, "Use data.market.bist dividends and capital_changes."),
    _non_bist(ownership.SPEC, "Use data.market.bist holders or etf_holders; no SEC holdings data."),
    _non_bist(filings.SPEC, "Use data.market.news with the BIST symbol for recent KAP filings."),
    _non_bist(digest.SPEC, "Use data.market.news for recent KAP disclosures; no Tauric digest."),
]

__all__ = [
    "EQUITY_DATA_SPECS",
    "comps",
    "corporate_actions",
    "digest",
    "earnings",
    "estimates",
    "filings",
    "fundamentals",
    "ownership",
    "ratios",
    "reference",
]
