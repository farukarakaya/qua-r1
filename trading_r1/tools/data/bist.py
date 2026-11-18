# SPDX-License-Identifier: Apache-2.0

"""Daily Borsa İstanbul prices via the optional borsapy adapter."""

from __future__ import annotations

import asyncio
import datetime as dt
import math
import re
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


def _history(ticker: str, first: dt.date, last: dt.date) -> list[dict[str, Any]]:
    try:
        import borsapy
    except ImportError:
        raise ToolError("BIST prices require the 'bist' extra: uv sync --extra bist.") from None

    try:
        frame = borsapy.Ticker(ticker).history(
            interval="1d",
            start=first.isoformat(),
            end=last.isoformat(),
            adjust=True,
            actions=False,
            auto_adjust=False,
        )
    except Exception:
        raise ToolError(f"{SOURCE} could not fetch history for XIST:{ticker}.") from None
    zone = istanbul()
    rows: dict[str, dict[str, Any]] = {}
    try:
        if frame.empty:
            return []
        prices = frame[["Open", "High", "Low", "Close", "Volume"]]
        for stamp, *values in prices.itertuples(index=True, name=None):
            day = stamp.astimezone(zone).date() if stamp.tzinfo else stamp.date()
            if not first <= day <= last:
                continue
            date = day.isoformat()
            o, h, low, c, volume = map(float, values)
            if (
                not all(math.isfinite(v) for v in (o, h, low, c, volume))
                or not (0 < low <= min(o, c) <= max(o, c) <= h and volume >= 0)
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
    return [rows[date] for date in sorted(rows)]


async def fetch_daily(ticker: str, first: dt.date, last: dt.date) -> list[dict[str, Any]]:
    return await asyncio.get_running_loop().run_in_executor(_POOL, _history, ticker, first, last)
