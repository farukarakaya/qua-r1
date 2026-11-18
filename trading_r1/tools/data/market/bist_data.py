# SPDX-License-Identifier: Apache-2.0

"""BIST datasets whose native fields do not match the Tauric equity schemas."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, FiniteFloat

from trading_r1.contract.spec import Output, Provenance, Result, ToolError, ToolSpec, hint
from trading_r1.tools.data import bist
from trading_r1.tools.data.common import Symbol

_STATEMENTS = {
    "balance_sheet": "get_balance_sheet",
    "income_statement": "get_income_stmt",
    "cash_flow": "get_cashflow",
}
_IS = "İş Yatırım via borsapy"
_KAP = "KAP via borsapy"
_TARGETS = "Hedef Fiyat via borsapy"
_PROPERTIES = {
    "dividends": ("dividends", _IS),
    "capital_changes": ("splits", _IS),
    "holders": ("major_holders", _IS),
    "etf_holders": ("etf_holders", "TradingView via borsapy"),
    "filing_calendar": ("calendar", _KAP),
    "recommendation": ("recommendations", _IS),
    "recommendation_summary": ("recommendations_summary", _TARGETS),
    "analyst_targets": ("analyst_price_targets", _TARGETS),
    "isin": ("isin", "borsapy ISIN provider"),
}
_NOTES = {
    "dividends": "Provider-reported amounts in TRY per share; not adjusted here for later splits.",
    "capital_changes": "Native paid rights and bonus capital percentages, not split multipliers.",
    "filing_calendar": "Expected KAP filing windows, not confirmed earnings dates.",
    "disclosure": "Text from the KAP disclosure page; attachments are not included.",
    "screener": "Market capitalization is in million USD; yields and margins are percentages.",
}


class BistDataRequest(BaseModel):
    dataset: Literal[
        "profile",
        "isin",
        "balance_sheet",
        "income_statement",
        "cash_flow",
        "dividends",
        "capital_changes",
        "holders",
        "etf_holders",
        "filing_calendar",
        "recommendation",
        "recommendation_summary",
        "analyst_targets",
        "disclosure",
        "companies",
        "indices",
        "index_components",
        "screener",
    ]
    symbol: Symbol | None = Field(None, description="Qualified BIST symbol, e.g. XIST:THYAO.")
    cadence: Literal["annual", "quarterly"] = "annual"
    financial_group: Literal["XI_29", "UFRS"] | None = None
    periods: int = Field(4, ge=1, le=20, description="Number of financial statement periods.")
    limit: int = Field(
        20, ge=1, le=250, description="Maximum rows other than statement line items."
    )
    disclosure_id: int | None = Field(None, gt=0, description="Numeric ID from a KAP article URL.")
    sector: str | None = Field(None, max_length=80, description="Optional screener sector filter.")
    index: Symbol | None = Field(None, description="Screener index code, e.g. XIST:XU100.")
    pe_max: FiniteFloat | None = None
    dividend_yield_min: FiniteFloat | None = Field(
        None, description="Minimum screener yield, in percent."
    )


# An open record with provider-native column names; the renderer can still budget rows.
_Row = Annotated[dict[str, Any], Field(json_schema_extra={"additionalProperties": {}})]


class BistDataOutput(Output):
    dataset: str
    symbol: str | None = None
    rows: Annotated[list[_Row], hint(rows=24)]
    notes: list[str] = []


def _rows(data: Any, limit: int | None = None) -> list[dict[str, Any]]:
    if data is None:
        return []
    if isinstance(data, dict):
        return [data] if any(v is not None for v in data.values()) else []
    if isinstance(data, list):
        return data[:limit]
    # Pandas handles missing values, timestamp conversion and numpy scalars in the optional extra.
    if data.index.name is not None:
        data = data.reset_index()
    if limit is not None:
        data = data.head(limit)
    return json.loads(data.to_json(orient="records", date_format="iso"))


def _fetch(bp: Any, req: BistDataRequest, ticker: str | None) -> tuple[list[dict[str, Any]], str]:
    dataset = req.dataset
    if dataset == "companies":
        return _rows(bp.companies(), req.limit), _KAP
    if dataset == "indices":
        return _rows(bp.all_indices(), req.limit), "borsapy BIST index catalog"
    if dataset == "screener":
        index = (bist.bist_ticker(req.index) or req.index) if req.index else None
        members = None
        if index:
            members = set(bp.Index(index).component_symbols)
            if not members:
                raise ToolError(f"No BIST index constituents for {index}; screening was not run.")
        frame = bp.screen_stocks(
            sector=req.sector, pe_max=req.pe_max, dividend_yield_min=req.dividend_yield_min
        )
        if members is not None and not frame.empty:
            frame = frame[frame["symbol"].isin(members)]
        return _rows(frame, req.limit), _IS
    if dataset == "index_components":
        return _rows(bp.Index(ticker).components, req.limit), "borsapy BIST index components"
    if dataset == "profile":
        if ticker in bp.indices():
            return _rows(bp.Index(ticker).info), bist.SOURCE
        return _rows(bp.Ticker(ticker).info.todict()), "borsapy BIST profile"

    stock = bp.Ticker(ticker)
    if dataset in _STATEMENTS:
        method = getattr(stock, _STATEMENTS[dataset])
        return _rows(
            method(
                quarterly=req.cadence == "quarterly",
                financial_group=req.financial_group,
                last_n=req.periods,
            )
        ), _IS
    if dataset == "disclosure":
        from bs4 import BeautifulSoup

        html = stock.get_news_content(req.disclosure_id)
        if not html:
            return [], _KAP
        page = BeautifulSoup(html, "html.parser")
        text = (page.find("main") or page).get_text(" ", strip=True)
        if not text:
            return [], _KAP
        return [
            {
                "id": req.disclosure_id,
                "url": f"https://www.kap.org.tr/tr/Bildirim/{req.disclosure_id}",
                "text": text[:12000],
                "truncated": len(text) > 12000,
            }
        ], _KAP
    prop, source = _PROPERTIES[dataset]
    data = getattr(stock, prop)
    return _rows({"isin": data} if dataset == "isin" else data, req.limit), source


async def _handler(req: BistDataRequest) -> Result:
    ticker = bist.bist_ticker(req.symbol) if req.symbol else None
    if not ticker and (req.symbol or req.dataset not in {"companies", "indices", "screener"}):
        raise ToolError("Use a qualified BIST symbol such as XIST:THYAO or THYAO.IS.")
    if req.dataset == "disclosure" and req.disclosure_id is None:
        raise ToolError("Provide a numeric KAP disclosure_id from data.market.news.")
    if req.dataset == "cash_flow" and req.financial_group == "UFRS":
        raise ToolError("The İş Yatırım bank format does not provide cash flow via borsapy.")
    subject = f"XIST:{ticker}" if ticker else "BIST"
    rows, source = await bist.run(subject, req.dataset, lambda bp: _fetch(bp, req, ticker))
    if not rows:
        raise ToolError(f"No {req.dataset} available from {source} for {subject}.")
    notes = []
    if req.dataset in _STATEMENTS:
        notes.append("Native İş Yatırım line items and reporting periods; not normalized SEC data.")
    if note := _NOTES.get(req.dataset):
        notes.append(note)
    disclosure = req.dataset == "disclosure"
    return Result(
        BistDataOutput(
            dataset=req.dataset, symbol=None if disclosure else subject, rows=rows, notes=notes
        ),
        Provenance(
            source=source,
            subject=f"KAP {req.disclosure_id}" if disclosure else subject,
            view=req.dataset,
        ),
    )


SPEC = ToolSpec(
    name="data_market_bist",
    display_name="data.market.bist",
    result_kind="data",
    tier="deferred",
    description=(
        "BIST financial statements, corporate actions, profile, ownership, analyst targets, "
        "KAP filing calendar, disclosure text, index members and screening via borsapy. "
        "Use XIST:THYAO or THYAO.IS for companies and XIST:XU100 for indices. "
        "Use data.market.news for recent KAP disclosures and data.market.prices for OHLCV. "
        "Statements preserve native line items; use financial_group=UFRS for banks. "
        "Use both dividends and capital_changes for corporate actions. "
        "No U.S. SEC concepts, analyst EPS forecasts, or confirmed earnings dates are inferred."
    ),
    request_schema=BistDataRequest,
    handler=_handler,
    output=BistDataOutput,
    tags=("BIST", "fundamentals", "disclosures", "actions", "screening", "ownership", "data"),
)
