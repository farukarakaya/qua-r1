# Copyright 2026 Tauric Research
# SPDX-License-Identifier: Apache-2.0

"""data.market.symbol_search: find the symbol for a name, across asset classes.

Resolves a company, instrument or partial symbol to Tauric's instruments. One
search over Tauric's instrument registry (listed securities, futures roots, currency pairs,
crypto pairs and index and macro series), optionally within one class. A match's symbol goes to
data.market.prices. Explicit BIST queries search borsapy instead.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

from trading_r1.contract.spec import Output, Provenance, Result, ToolError, ToolSpec
from trading_r1.contract.units import Code
from trading_r1.tools.data import bist
from trading_r1.tools.data.symbols import INDEX_SERIES
from trading_r1.tools.data.tauric import Tauric, provenance

_DESCRIPTION = (
    "Find the symbol for a company, fund, index, future, currency pair or crypto asset by name "
    "or partial symbol (e.g. 'Vistra', 'crude oil', 'euro dollar', 'S&P 500', 'bitcoin') in "
    "Tauric's registry (a maintained set of listings, not every listed company). asset_type "
    "'any' (default) ranks matches across classes; a class (equity, etf, fund, index, future, "
    "currency, cryptocurrency) searches within it. Each match gives symbol, instrument `key`, "
    "name and "
    "instrument type; pass the symbol to data_market_prices (a macro series key goes to "
    "data_macro_series). Use this instead of guessing a ticker. Short queries match best. "
    "For BIST, prefix a name or partial symbol with XIST: or use a .IS suffix; "
    "search stocks (equity) or indices (index) via borsapy, or use 'any' to look for stocks "
    "first and indices if none match."
)
# asset_type to the registry filters that select it
_FILTERS: dict[str, dict[str, str]] = {
    "any": {},
    "equity": {"kind": "security", "instrument_type": "stock"},
    "etf": {"kind": "security", "instrument_type": "etf"},
    "fund": {"kind": "security", "instrument_type": "fund"},
    "index": {"kind": "series", "instrument_type": "index"},
    "future": {"kind": "future"},
    "currency": {"kind": "fx"},
    "cryptocurrency": {"kind": "crypto"},
}
AssetType = Literal["any", "equity", "etf", "fund", "index", "future", "currency", "cryptocurrency"]
_CARET = {v: k for k, v in INDEX_SERIES.items() if k != "^SPX"}


class SymbolSearchRequest(BaseModel):
    """Input for a symbol search."""

    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)] = (
        Field(..., description="Name or partial symbol.")
    )
    asset_type: AssetType = Field("any", description="'any', or one class to search within.")
    limit: int = Field(8, ge=1, le=20, description="Most matches to return.")


def _match(item: dict[str, Any]) -> dict[str, Any]:
    key = item["key"]
    kind, _, rest = key.partition(":")
    cls = item.get("classification") or {}
    instrument_type = cls.get("instrument_type")
    if kind == "future":
        symbol = rest.removesuffix("/continuous").rsplit(":", 1)[-1] + "=F"
    elif kind == "series":
        symbol = _CARET.get(rest, key)
    else:
        symbol = rest
    return {
        "symbol": symbol,
        "key": key,
        "name": item.get("name"),
        "instrument_type": instrument_type,
    }


class Match(BaseModel):
    symbol: Code
    key: Code
    name: Code = None
    instrument_type: Code = None


class SymbolSearchOutput(Output):
    """The registry's matches, best first."""

    query: Code
    asset_type: Code
    matches: list[Match]


def _bist_query(query: str) -> str | None:
    if query.upper().startswith("XIST:"):
        query = query[5:].strip()
        return query[:-3].strip() if query.upper().endswith(".IS") else query
    if ":" not in query and query.upper().endswith(".IS"):
        return query[:-3].strip()
    return None


def _bist_matches(bp: Any, query: str, req: SymbolSearchRequest) -> tuple[list[Match], str]:
    def matches(rows: Any, code: str, name: str, kind: str) -> list[Match]:
        found: dict[str, Match] = {}
        for row in rows:
            if not row.get(code):
                continue
            try:
                ticker = bist.bist_ticker(f"XIST:{row[code]}")
            except ToolError:
                continue
            symbol = f"XIST:{ticker}"
            if symbol not in found:
                found[symbol] = Match(
                    symbol=symbol,
                    key=f"security:{symbol}",
                    name=row.get(name),
                    instrument_type=kind,
                )
            if len(found) >= req.limit:
                break
        return list(found.values())

    if req.asset_type != "index":
        rows = bp.search_companies(query).to_dict("records")
        stocks = matches(rows, "ticker", "name", "stock")
        if stocks or req.asset_type == "equity":
            return stocks, "KAP (via borsapy)"
    rows = bp.search(query, type="index", exchange="BIST", limit=req.limit, full_info=True)
    supported = set(bp.indices())  # Use the same index list as prices and quotes.
    indices = (
        row for row in rows if row.get("exchange") == "BIST" and row.get("symbol") in supported
    )
    return matches(indices, "symbol", "description", "index"), bist.SOURCE


async def _bist_search(req: SymbolSearchRequest, query: str) -> Result:
    if not query:
        raise ToolError("Give a BIST name or ticker, e.g. XIST:THYAO or THYAO.IS.")
    if req.asset_type not in {"any", "equity", "index"}:
        raise ToolError("BIST symbol search supports asset_type 'any', 'equity' or 'index'.")
    matches, source = await bist.run(
        req.query, "symbol search", lambda bp: _bist_matches(bp, query, req)
    )
    if not matches:
        raise ToolError(f"No BIST symbol matched '{req.query}': try a shorter name or ticker.")
    return Result(
        SymbolSearchOutput(query=req.query, asset_type=req.asset_type, matches=matches),
        Provenance(**provenance(subject=req.query, source=source)),
    )


async def _handler(req: SymbolSearchRequest) -> Result:
    if (query := _bist_query(req.query)) is not None:
        return await _bist_search(req, query)
    async with Tauric() as api:
        body = await api.get(
            "/instruments", q=req.query, limit=req.limit, **_FILTERS[req.asset_type]
        )
    # a futures root is listed as the root and as its continuous series: one match each
    items = [
        i
        for i in body.get("data", [])
        if not i["key"].startswith("future:") or i["key"].endswith("/continuous")
    ]
    matches = [_match(i) for i in items]
    if not matches:
        hint = "" if req.asset_type == "any" else ", or asset_type 'any'"
        raise ToolError(
            f"no symbol matched '{req.query}': try a fuller or simpler name in English{hint}, "
            "or find the ticker with the web `search` tool"
        )
    return Result(
        SymbolSearchOutput(query=req.query, asset_type=req.asset_type, matches=matches),
        Provenance(**provenance(subject=req.query)),
    )


SPEC = ToolSpec(
    name="data_market_symbol_search",
    display_name="data.market.symbol_search",
    result_kind="data",
    tier="deferred",
    description=_DESCRIPTION,
    request_schema=SymbolSearchRequest,
    handler=_handler,
    output=SymbolSearchOutput,
    tags=("market", "symbols", "search", "lookup", "ticker", "data"),
)
