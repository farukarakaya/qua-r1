# SPDX-License-Identifier: Apache-2.0

"""Shared Borsa İstanbul routing and borsapy adapter."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from trading_r1.contract.spec import ToolError
from trading_r1.tools.data.symbols import canonical

SOURCE = "borsapy (TradingView BIST)"
FX_SOURCE = "borsapy (FX)"
FX_BASES = frozenset({"USD", "EUR", "GBP", "CHF"})
MAX_LOOKBACK_DAYS = 5000
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bist")
_SCREEN_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bist-screen")
_SCREEN_SESSION_LOCK = Lock()
_TRY_FX = re.compile(r"^([A-Z]{3})[-/_]?TRY$")

SCREEN_SOURCE = "İş Yatırım stock screener via borsapy"
# Raw getScreenerDataNEW IDs (HisseHiyerasi.TanimID), not the general-criteria picker IDs.
# In particular 8 is TRY cap, 9 is USD cap, and 60 is a target-price change in this endpoint.
_SCREEN_CRITERIA = {
    "market_cap_usd": "9",
    "avg_volume_3m_usd": "26",
    "pe": "28",
    "ev_ebitda": "29",
    "pb": "30",
    "dividend_yield": "33",  # reported year (ID1)
    "upside_potential": "61",
    "net_income_growth": "73",  # first forecast year (ID2)
    "net_margin": "119",  # first forecast year (ID2)
    "roe": "422",
}
_INDEX_ALIASES = {"BIST30": "XU030", "BIST50": "XU050", "BIST100": "XU100"}


def try_fx_base(symbol: str) -> str | None:
    """Return the base currency (e.g. 'USD') when `symbol` names a borsapy-supported TRY pair."""
    name = symbol.strip().upper()
    if name == "TRY=X":
        return "USD"
    name = name.removeprefix("FX:").removesuffix("=X")
    if (match := _TRY_FX.fullmatch(name)) and match.group(1) in FX_BASES:
        return match.group(1)
    return None


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
                "Could not import borsapy. Reinstall the backend dependencies."
            ) from None
        try:
            return fetch(borsapy)
        except ToolError:
            raise
        except Exception:
            raise ToolError(f"borsapy could not fetch {dataset} for {subject}.") from None

    return await asyncio.get_running_loop().run_in_executor(_POOL, call)


def _screen_sector(provider: Any, sector: str) -> tuple[str, str]:
    from bs4 import BeautifulSoup

    def key(label: str) -> str:
        return " ".join(label.casefold().replace("i\u0307", "i").replace("ı", "i").split())

    page = BeautifulSoup(provider._get(provider.PAGE_URL).content, "html.parser")
    # SharePoint changes the generated prefix of this dropdown's ID.
    options = page.select("select[id$='_ddlStockSector'] option[value]")
    if not options:
        raise ToolError(f"{SCREEN_SOURCE} did not return its sector list.")
    for option in options:
        code = option["value"]
        name = option.get_text(" ", strip=True)
        if code and key(sector) in (code, key(name)):
            return code, name
    raise ToolError("Use an İş Yatırım sector name or code, e.g. Bankacılık or 0001.")


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _screen_metric(
    provider: Any, metric: str, sector_id: str | None, recommendation: str | None
) -> dict[str, tuple[str, float | None]]:
    criterion = _SCREEN_CRITERIA[metric]
    payload = {
        "sektor": sector_id or "",
        "endeks": "",
        "takip": "",
        "oneri": recommendation or "",
        "criterias": [[criterion, "-1000000000", "1000000000", "False"]],
        "lang": "1055",
    }
    # Do not use borsapy's parsed cache: a missing `d` field is cached there as an empty list.
    key = f"qua-r1:bist-screen:{json.dumps(payload, sort_keys=True)}"
    if (cached := provider._cache_get(key)) is not None:
        return cached
    try:
        response = provider._post(
            provider.SCREENER_URL, json=payload, headers=provider._get_headers()
        )
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("d"), str):
            raise ValueError
        records = json.loads(body["d"])
        if not isinstance(records, list):
            raise ValueError
        rows = {}
        for row in records:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("Hisse"), str)
                or criterion not in row
            ):
                raise ValueError
            ticker, _, name = row["Hisse"].partition(" - ")
            ticker = ticker.strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{1,12}", ticker) or ticker in rows:
                raise ValueError
            raw = row[criterion]
            value = _finite_number(raw)
            if value is None and raw not in (None, ""):
                raise ValueError
            rows[ticker] = (name.strip() or ticker, value)
    except (ValueError, RecursionError):
        raise ToolError(f"{SCREEN_SOURCE} returned invalid data for {metric}.") from None
    except httpx.HTTPError:
        raise ToolError(f"{SCREEN_SOURCE} could not fetch {metric}.") from None
    if metric == "market_cap_usd" and not rows and not sector_id and not recommendation:
        raise ToolError(f"{SCREEN_SOURCE} returned an empty BIST universe.")
    provider._cache_set(key, rows, 900)
    return rows


def _screen_stocks(
    bp: Any,
    index: str | None,
    sector: str | None,
    recommendation: str | None,
    required: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    from borsapy._providers.isyatirim_screener import get_screener_provider

    members = None
    if index:
        name = index.upper().replace(" ", "").removeprefix("XIST:")
        code = _INDEX_ALIASES.get(name, name)
        members = set(bp.Index(code).component_symbols)
        if not members:
            raise ToolError(f"No BIST index constituents for {code}; screening was not run.")
    with _SCREEN_SESSION_LOCK:
        provider = get_screener_provider()
        provider._init_session()
    sector_id, sector_name = _screen_sector(provider, sector) if sector else (None, None)

    # İş Yatırım intersects criteria even with required=False. Left-join separate requests
    # onto the market-cap universe, keeping outages distinct from missing observations.
    universe = _screen_metric(provider, "market_cap_usd", sector_id, recommendation)
    rows = {
        ticker: {
            "symbol": f"XIST:{ticker}",
            "name": name,
            "sector": sector_name,
            "market_cap_usd": cap,
        }
        for ticker, (name, cap) in universe.items()
        if members is None or ticker in members
    }
    if not rows:
        return [], []

    def fetch(metric: str) -> dict[str, tuple[str, float | None]] | None:
        try:
            return _screen_metric(provider, metric, sector_id, recommendation)
        except ToolError:
            if metric in required:
                raise
            return None

    unavailable = []
    metrics = [metric for metric in _SCREEN_CRITERIA if metric != "market_cap_usd"]
    for metric, records in zip(metrics, _SCREEN_POOL.map(fetch, metrics), strict=True):
        if records is None:
            unavailable.append(metric)
        else:
            for ticker in rows.keys() & records.keys():
                rows[ticker][metric] = records[ticker][1]
    return list(rows.values()), unavailable


async def screen_stocks(
    *,
    index: str | None,
    sector: str | None,
    recommendation: str | None,
    required: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    return await run(
        index or "BIST",
        "stock screen",
        lambda bp: _screen_stocks(bp, index, sector, recommendation, required),
    )


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


def _fx_history(
    bp: Any, base: str, first: dt.date, last: dt.date
) -> tuple[list[dict[str, Any]], list[str]]:
    frame = bp.FX(base).history(interval="1d", start=first.isoformat(), end=last.isoformat())
    zone = istanbul()
    rows: dict[str, dict[str, Any]] = {}
    close_only: list[str] = []
    try:
        if frame.empty:
            return [], []
        prices = frame.reindex(columns=["Open", "High", "Low", "Close"])
        for stamp, *values in prices.itertuples(index=True, name=None):
            day = stamp.astimezone(zone).date() if stamp.tzinfo else stamp.date()
            if not first <= day <= last:
                continue
            date = day.isoformat()
            if date in rows:
                raise ToolError(f"{FX_SOURCE} returned a duplicate {base}TRY date: {date}.")
            o, h, low, c = map(_finite_number, values)
            if c is None or c <= 0:
                raise ToolError(f"{FX_SOURCE} returned an invalid {base}TRY close on {date}.")
            if (
                o is None
                or h is None
                or low is None
                or not (0 < low <= min(o, c) <= max(o, c) <= h)
            ):
                close_only.append(date)
                o = h = low = None
            rows[date] = {
                "date": date,
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": None,
            }
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        raise ToolError(f"{FX_SOURCE} returned invalid daily bars for {base}TRY.") from None
    return [rows[date] for date in sorted(rows)], sorted(close_only)


async def fetch_fx_daily(
    base: str, first: dt.date, last: dt.date
) -> tuple[list[dict[str, Any]], list[str]]:
    return await run(f"{base}TRY", "FX history", lambda bp: _fx_history(bp, base, first, last))
