# SPDX-License-Identifier: Apache-2.0

"""Shared Borsa İstanbul routing and optional borsapy adapter."""

from __future__ import annotations

import asyncio
import datetime as dt
import math
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from trading_r1.contract.spec import ToolError
from trading_r1.tools.data.symbols import canonical

SOURCE = "borsapy (TradingView BIST)"
MAX_LOOKBACK_DAYS = 5000
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bist")


def bist_ticker(symbol: str) -> str | None:
    name = canonical(symbol)
    if name.startswith("XIST:") or (":" not in name and name.endswith(".IS")):
        ticker = name.removeprefix("XIST:").removesuffix(".IS")
        if not re.fullmatch(r"[A-Z0-9]{1,12}", ticker):
            raise ToolError("Use a BIST ticker like XIST:THYAO or THYAO.IS.")
        return ticker
    return None


def istanbul() -> ZoneInfo:
    try:
        return ZoneInfo("Europe/Istanbul")
    except ZoneInfoNotFoundError:
        raise ToolError("BIST prices require Istanbul time zone data (install tzdata).") from None


async def run[T](subject: str, dataset: str, fetch: Callable[[Any], T]) -> T:
    def call() -> T:
        try:
            import borsapy
        except ImportError:
            raise ToolError(
                "BIST data requires the 'bist' extra: uv run --locked --extra bist trading-r1."
            ) from None
        try:
            return fetch(borsapy)
        except ToolError:
            raise
        except Exception:
            raise ToolError(f"borsapy could not fetch {dataset} for {subject}.") from None

    return await asyncio.get_running_loop().run_in_executor(_POOL, call)


def _history(
    bp: Any, ticker: str, first: dt.date, last: dt.date
) -> tuple[list[dict[str, Any]], bool]:
    is_index = ticker in bp.indices()
    kwargs = {} if is_index else {"adjust": True, "actions": False, "auto_adjust": False}
    instrument = bp.Index(ticker) if is_index else bp.Ticker(ticker)
    frame = instrument.history(
        interval="1d", start=first.isoformat(), end=last.isoformat(), **kwargs
    )
    zone = istanbul()
    rows: dict[str, dict[str, Any]] = {}
    try:
        if frame.empty:
            return [], is_index
        prices = frame[["Open", "High", "Low", "Close", "Volume"]]
        for stamp, *values in prices.itertuples(index=True, name=None):
            day = stamp.astimezone(zone).date() if stamp.tzinfo else stamp.date()
            if not first <= day <= last:
                continue
            date = day.isoformat()
            o, h, low, c = map(float, values[:4])
            volume = float(values[4])
            if is_index and not math.isfinite(volume):
                volume = None  # An index level need not have a reported volume.
            if (
                not all(math.isfinite(v) for v in (o, h, low, c))
                or not (0 < low <= min(o, c) <= max(o, c) <= h)
                or (volume is not None and (not math.isfinite(volume) or volume < 0))
                or date in rows
            ):
                raise ValueError("invalid or duplicate bar")
            rows[date] = {
                "date": date,
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": volume,
            }
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        raise ToolError(f"{SOURCE} returned invalid daily bars for XIST:{ticker}.") from None
    return [rows[date] for date in sorted(rows)], is_index


async def fetch_daily(
    ticker: str, first: dt.date, last: dt.date
) -> tuple[list[dict[str, Any]], bool]:
    return await run(f"XIST:{ticker}", "history", lambda bp: _history(bp, ticker, first, last))
