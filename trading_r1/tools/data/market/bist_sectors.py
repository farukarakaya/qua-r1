# Copyright 2026 Tauric Research
# SPDX-License-Identifier: Apache-2.0

"""Sector rotation for Borsa İstanbul in TRY and USD."""

from __future__ import annotations

import asyncio
import datetime as dt
import statistics
from typing import Annotated, Literal

from pydantic import Field, field_validator

from trading_r1.contract.spec import Output, Provenance, Result, ToolError, ToolSpec, hint
from trading_r1.contract.units import Code, Count, Day, Fraction, Number, Text
from trading_r1.tools.compute.risk.drawdown import max_drawdown
from trading_r1.tools.compute.risk.volatility import realized_vol
from trading_r1.tools.data.market.aligned_prices import align
from trading_r1.tools.data.pricing import EQUITY, PriceWindow, fetch_bars

SECTOR_INDICES = {
    "XBANK": "Banks",
    "XUSIN": "Industrials",
    "XUTEK": "Technology",
    "XTCRT": "Trade/Retail",
    "XULAS": "Transportation",
    "XILTM": "Telecom",
    "XGIDA": "Food & Beverage",
    "XKMYA": "Chemicals/Refining",
    "XELKT": "Electricity",
    "XSGRT": "Insurance",
    "XHOLD": "Holdings",
    "XGMYO": "REITs",
    "XMANA": "Basic Metals",
    "XMADN": "Mining",
    "XTEKS": "Textiles",
    "XTRZM": "Tourism",
}
BENCHMARK = "XIST:XU100"
FX = "USDTRY=X"


class BistSectorsRequest(PriceWindow):
    interval: Literal["1d", "1wk", "1mo"] = Field(
        "1d", description="Daily, weekly or monthly bars. FX beta always uses daily returns."
    )
    sectors: list[str] | None = Field(
        None,
        min_length=1,
        max_length=len(SECTOR_INDICES),
        description="BIST sector codes such as XBANK or XIST:XUTEK. Omit for all 16.",
    )

    @field_validator("sectors")
    @classmethod
    def _sectors(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        codes = list(dict.fromkeys(s.strip().upper().removeprefix("XIST:") for s in v))
        if any(s not in SECTOR_INDICES for s in codes):
            raise ValueError("Use BIST sector codes: " + ", ".join(SECTOR_INDICES))
        return codes


class BistSector(Output):
    symbol: Code
    sector: Text
    try_return: Fraction
    usd_return: Fraction
    excess_usd_return: Fraction
    usd_volatility: Fraction
    usd_max_drawdown: Fraction
    fx_beta: Number


class BistSectorsOutput(Output):
    start: Day
    end: Day
    n_bars: Count
    n_fx_returns: Count
    interval: Code
    sectors: Annotated[list[BistSector], hint(rows=len(SECTOR_INDICES))]
    benchmark: BistSector
    failed: dict[str, Text] = {}
    notes: list[Text] = []


def _sample(dates: list[str], interval: str) -> list[int]:
    """Last shared session of each week or month, not separate last closes per symbol."""
    if interval == "1d":
        return list(range(len(dates)))
    groups = {}
    for i, date in enumerate(dates):
        day = dt.date.fromisoformat(date)
        key = day.isocalendar()[:2] if interval == "1wk" else (day.year, day.month)
        groups[key] = i
    return list(groups.values())


async def _handler(req: BistSectorsRequest) -> Result:
    symbols = [*(f"XIST:{s}" for s in req.sectors or SECTOR_INDICES), BENCHMARK, FX]
    results = await asyncio.gather(
        *(fetch_bars(sym, req.period, "1d", req.start, req.end) for sym in symbols),
        return_exceptions=True,
    )
    fetched = {}
    failed = {}
    for symbol, result in zip(symbols, results, strict=True):
        if isinstance(result, ToolError):
            failed[symbol] = str(result)
        elif isinstance(result, BaseException):
            raise result
        elif not result.rows:
            failed[symbol] = "No price bars in the requested window."
        else:
            fetched[symbol] = result
    if BENCHMARK not in fetched or FX not in fetched or len(fetched) < 3:
        raise ToolError(
            "BIST sector comparison requires XU100, USDTRY and at least one sector. Failed: "
            + "; ".join(f"{sym}: {reason}" for sym, reason in failed.items())
        )

    rows = {sym: b.rows for sym, b in fetched.items()}
    dates, prices = align(
        rows, {sym: [r["close"] for r in bars] for sym, bars in rows.items()}, False
    )
    sampled = _sample(dates, req.interval)
    if len(sampled) < 2:
        available = f" Failed indices: {', '.join(failed)}." if failed else ""
        raise ToolError(f"Fewer than two shared {req.interval} bars. Widen the window.{available}")
    first, last = sampled[0], sampled[-1]
    fx = prices[FX]
    # A missing sector or FX close must not turn a multi-session move into a daily return.
    sessions = {d: i for i, d in enumerate(sorted({r["date"] for r in rows[BENCHMARK]}))}
    daily_ends = [
        i for i in range(first + 1, last + 1) if sessions[dates[i]] == sessions[dates[i - 1]] + 1
    ]

    def daily_returns(levels: list[float]) -> list[float]:
        return [levels[i] / levels[i - 1] - 1 for i in daily_ends]

    fx_returns = daily_returns(fx)
    fx_variance = statistics.variance(fx_returns) if len(fx_returns) >= 2 else 0.0
    benchmark_usd = (prices[BENCHMARK][last] / fx[last]) / (
        prices[BENCHMARK][first] / fx[first]
    ) - 1
    per_year = EQUITY.bars_per_year(req.interval)

    def row(symbol: str, label: str) -> BistSector:
        levels = prices[symbol]
        usd = [levels[i] / fx[i] for i in sampled]
        usd_return = usd[-1] / usd[0] - 1
        beta = None
        if fx_variance:
            beta = statistics.covariance(daily_returns(levels), fx_returns) / fx_variance
        return BistSector(
            symbol=symbol,
            sector=label,
            try_return=levels[last] / levels[first] - 1,
            usd_return=usd_return,
            excess_usd_return=usd_return - benchmark_usd,
            usd_volatility=realized_vol(usd, per_year)[1],
            usd_max_drawdown=max_drawdown(usd),
            fx_beta=beta,
        )

    sectors = [
        row(s, SECTOR_INDICES[s.removeprefix("XIST:")]) for s in prices if s not in (BENCHMARK, FX)
    ]
    notes = [
        "BIST sector and benchmark returns are price returns, excluding cash dividends.",
        "FX beta is historical sensitivity to TRY depreciation (rising USDTRY), not a forecast. "
        "Matching dates do not imply simultaneous closes.",
    ]
    if not fx_variance:
        notes.append(
            "FX beta is unavailable: too few daily returns or no variation in USDTRY returns."
        )
    notes.extend(dict.fromkeys(n for b in fetched.values() for n in b.limits))
    out = BistSectorsOutput(
        start=dates[first],
        end=dates[last],
        n_bars=len(sampled),
        n_fx_returns=len(fx_returns),
        interval=req.interval,
        sectors=sorted(sectors, key=lambda s: s.usd_return, reverse=True),
        benchmark=row(BENCHMARK, "BIST 100"),
        failed=failed,
        notes=notes,
    )
    sources = ", ".join(dict.fromkeys(b.source for b in fetched.values()))
    return Result(out, Provenance(source=sources, subject="BIST sectors", as_of=out.end))


SPEC = ToolSpec(
    name="data_market_bist_sectors",
    display_name="data.market.bist_sectors",
    result_kind="data",
    tier="deferred",
    description=(
        "BIST sector indices ranked by USD price return, with TRY return, USD excess return "
        "against BIST 100, USD volatility, USD drawdown and empirical FX beta versus USDTRY. "
        "Returns exclude cash dividends; all successful indices and FX share dates. FX beta "
        "uses only consecutive XU100 sessions, even for weekly or monthly bars. "
        "Defaults to one year of daily bars and all 16 sectors."
    ),
    request_schema=BistSectorsRequest,
    handler=_handler,
    output=BistSectorsOutput,
    tags=("market", "bist", "sectors", "rotation", "fx", "data"),
)
