# Copyright 2026 Tauric Research
# SPDX-License-Identifier: Apache-2.0

"""data.market.quotes: current quotes for many instruments in one request.

A watchlist or portfolio question ("what moved overnight?") needs the latest price and day change
of several names across asset classes. Tauric serves non-BIST instruments in one batch;
explicitly qualified BIST symbols use borsapy.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math
from contextlib import suppress
from typing import Any

from pydantic import BaseModel, Field

from trading_r1.contract.spec import Output, Provenance, Result, ToolError, ToolSpec
from trading_r1.contract.units import Code, Count, Day, Percent, Price, Text
from trading_r1.tools.data import bist
from trading_r1.tools.data.common import is_num
from trading_r1.tools.data.symbols import resolve
from trading_r1.tools.data.tauric import Tauric, provenance

_MAX_SYMBOLS = 50
_DESCRIPTION = (
    "Current quotes for up to 50 instruments: last price, day change (in percent "
    "as published), the previous close, the day's range and volume, the 52-week range, the "
    "market state (regular, pre/post market, closed) and when the price was struck. Takes "
    "tickers, indices (^GSPC), futures (CL=F), FX (EURUSD=X) and crypto (BTC-USD). For BIST "
    "use XIST:THYAO or THYAO.IS; borsapy quotes may have fewer fields. Use for "
    "a watchlist, a portfolio's holdings or a market overview; for one company's valuation "
    "use data.equity.reference, for history data.market.prices."
)


class MarketQuotesRequest(BaseModel):
    """Input for the batch-quote tool."""

    symbols: list[str] = Field(
        ...,
        min_length=1,
        max_length=_MAX_SYMBOLS,
        description="Symbols to quote, e.g. ['NVDA', 'AAPL', '^GSPC', 'CL=F', 'BTC-USD'].",
    )


class Quote(BaseModel):
    price: Price
    change: Price = None
    change_pct: Percent = None  # the day's change in percent, as published
    previous_close: Price = None
    day_high: Price = None
    day_low: Price = None
    volume: Count = None
    high_52w: Price = None
    low_52w: Price = None
    currency: Code = None
    market_state: Code = None  # regular, pre_market, post_market, closed; none for 24h markets
    as_of: Day = None  # when the price was struck (UTC)
    session_date: Day = None


class MarketQuotesOutput(Output):
    """One quote per symbol, in the order asked; `left_out` names each symbol that has none and
    why."""

    quotes: dict[str, Quote]
    left_out: dict[str, Text] = {}
    notes: list[Text] = []


def _quote(q: dict[str, Any]) -> Quote:
    day = q.get("day") or {}
    span = q.get("range_52w") or {}
    raw_change = q.get("change")
    if isinstance(raw_change, dict):
        change, change_pct = raw_change.get("absolute"), raw_change.get("percent")
    else:
        change, change_pct = raw_change, q.get("change_percent")
    return Quote(
        price=q.get("price"),
        change=change,
        change_pct=change_pct,
        previous_close=(q.get("previous_close") or {}).get("value"),
        day_high=day.get("high"),
        day_low=day.get("low"),
        volume=day.get("volume"),
        high_52w=span.get("high"),
        low_52w=span.get("low"),
        currency=(q.get("instrument") or {}).get("currency"),
        market_state=q.get("market_state"),
        as_of=q.get("price_time") or q.get("source_time"),
        session_date=q.get("session_date"),
    )


def _quote_map(body: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(body, dict):
        return {}
    if isinstance(raw := body.get("data"), list):
        mapped: dict[str, dict[str, Any]] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = item.get("key") or (item.get("instrument") or {}).get("key")
            if isinstance(key, str) and key:
                mapped[key] = item
        return mapped
    return body["quotes"] if isinstance(body.get("quotes"), dict) else {}


def _error_map(body: Any) -> dict[str, str]:
    if not isinstance(body, dict):
        return {}
    raw = body.get("errors")
    if isinstance(raw, list):
        mapped: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            err = item.get("error")
            msg = err.get("message") if isinstance(err, dict) else item.get("message")
            if isinstance(key, str) and isinstance(msg, str) and msg:
                mapped[key] = msg
        return mapped
    if isinstance(raw, dict):
        return {
            key: err["message"]
            for key, err in raw.items()
            if isinstance(err, dict) and isinstance(err.get("message"), str)
        }
    return {}


def _bist_quote(bp: Any, ticker: str) -> Quote | None:
    is_index = ticker in bist.index_symbols(bp)
    info = (bp.Index(ticker) if is_index else bp.Ticker(ticker)).info
    # Read only fields in TradingView's basic quote dict (including prev_close and timestamp).
    last = info.get("last")
    if not is_num(last) or not math.isfinite(last) or last <= 0:
        return None
    as_of = None
    stamp = info.get("timestamp")  # TradingView's last-price epoch seconds, when available
    if is_num(stamp) and stamp > 0:
        with suppress(OSError, OverflowError, ValueError):
            as_of = dt.datetime.fromtimestamp(stamp, dt.UTC).isoformat().replace("+00:00", "Z")
    return Quote(
        price=last,
        change=info.get("change"),
        change_pct=info.get("change_percent"),
        previous_close=info.get("prev_close"),
        day_high=info.get("high"),
        day_low=info.get("low"),
        volume=info.get("volume"),
        currency=info.get("currency") or (None if is_index else "TRY"),
        as_of=as_of,
    )


def _bist_fx_quote(bp: Any, base: str) -> Quote | None:
    info = bp.FX(base).current
    if not isinstance(info, dict):
        return None
    last = info.get("last")
    if not is_num(last) or not math.isfinite(last) or last <= 0:
        return None
    as_of = None
    stamp = info.get("update_time")
    if isinstance(stamp, dt.datetime):
        zone = stamp.tzinfo or bist.istanbul()
        as_of = stamp.replace(tzinfo=zone).astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    elif isinstance(stamp, str) and stamp.strip():
        as_of = stamp.strip()
    return Quote(
        price=last,
        day_high=info.get("high"),
        day_low=info.get("low"),
        currency="TRY",
        as_of=as_of,
    )


async def _handler(req: MarketQuotesRequest) -> Result:
    asked: dict[str, list[str]] = {}  # instrument key to the spellings asked
    labels: dict[str, str] = {}
    tickers: dict[str, str] = {}
    fx_bases: dict[str, str] = {}
    left_out: dict[str, str] = {}
    notes: list[str] = []
    for symbol in req.symbols:
        try:
            if ticker := bist.bist_ticker(symbol):
                label = f"XIST:{ticker}"
                key = f"security:{label}"
                tickers[key] = ticker
            elif base := bist.try_fx_base(symbol):
                label = f"{base}TRY"
                key = f"fx:{label}"
                fx_bases[key] = base
            else:
                inst = resolve(symbol)
                if inst.note and inst.note not in notes:
                    notes.append(inst.note)
                key, label = inst.key, inst.label
        except ToolError as err:
            left_out[symbol] = str(err)
            continue
        if symbol not in asked.setdefault(key, []):
            asked[key].append(symbol)
        labels.setdefault(key, label)
    if not asked:
        raise ToolError("no symbol could be quoted: " + "; ".join(left_out.values()))

    body, tauric_error = {}, None
    if keys := [key for key in asked if key not in tickers and key not in fx_bases]:
        try:
            async with Tauric() as api:
                body = await api.post("/quotes", {"instruments": keys})
            notes += api.limits
        except ToolError as err:
            if not tickers and not fx_bases:
                raise
            tauric_error = str(err)
    data, errors = _quote_map(body), _error_map(body)

    local_calls = [
        *(
            (key, bist.run(labels[key], "quote", lambda bp, t=ticker: _bist_quote(bp, t)))
            for key, ticker in tickers.items()
        ),
        *(
            (key, bist.run(labels[key], "FX quote", lambda bp, b=base: _bist_fx_quote(bp, b)))
            for key, base in fx_bases.items()
        ),
    ]
    native = dict(
        zip(
            (key for key, _ in local_calls),
            await asyncio.gather(*(call for _, call in local_calls), return_exceptions=True),
            strict=True,
        )
    )
    quotes: dict[str, Quote] = {}
    sources: dict[str, None] = {}
    dates: list[str] = []
    for key, spellings in asked.items():
        why = None
        if key in native:
            quote = native[key]
            if isinstance(quote, ToolError):
                why, quote = str(quote), None
            elif isinstance(quote, BaseException):
                raise quote
            source = bist.FX_SOURCE if key in fx_bases else bist.SOURCE
        else:
            raw = data.get(key) or {}
            quote = _quote(raw) if raw.get("price") is not None else None
            why = tauric_error or errors.get(key)
            source = "Tauric"
        if quote is not None:
            # two instruments can share a label (a stock and a currency pair): the key tells apart
            label = labels[key] if labels[key] not in quotes else key
            quotes[label] = quote
            sources[source] = None
            if day := quote.session_date or (quote.as_of if key in native else None):
                dates.append(day[:10])
        else:
            left_out.update({symbol: why or "no price right now" for symbol in spellings})
    if not quotes:
        raise ToolError("no quotes available: " + "; ".join(left_out.values()))
    return Result(
        MarketQuotesOutput(quotes=quotes, left_out=left_out, notes=notes),
        Provenance(
            **provenance(
                subject=", ".join(quotes),
                view="quotes",
                as_of=max(dates, default=None),
                source=" + ".join(sources),
            )
        ),
    )


SPEC = ToolSpec(
    name="data_market_quotes",
    display_name="data.market.quotes",
    result_kind="data",
    tier="deferred",
    description=_DESCRIPTION,
    request_schema=MarketQuotesRequest,
    handler=_handler,
    output=MarketQuotesOutput,
    tags=("market", "quotes", "watchlist", "portfolio", "prices", "data"),
)
