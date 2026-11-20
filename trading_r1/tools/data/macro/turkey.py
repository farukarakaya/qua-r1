# Copyright 2026 Tauric Research
# SPDX-License-Identifier: Apache-2.0

"""Turkish CPI, CBRT EVDS indicators and USD sovereign borrowing costs."""

from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

from trading_r1.contract.spec import Output, Provenance, Result, ToolError, ToolSpec, hint
from trading_r1.contract.units import Code, Day, Number, Percent, Points, Text
from trading_r1.tools.data import bist
from trading_r1.tools.data.common import months_back

# Key, CBRT series code, unit. Gross FX reserves exclude gold.
_INDICATORS = {
    "funding_rate": ("TP.APIFON4", "%"),
    "reer_cpi": ("TP.RK.T1.Y", "index (2025=100)"),
    "gross_fx_reserves_usd_m": ("TP.AB.C2", "USD millions"),
    "try_deposit_3m": ("TP.TRY.MT02", "%"),
    "try_commercial_loan": ("TP.KTF17", "%"),
}
_BUCKETS = {"1-3y": (1, 3, 2), "3-7y": (3, 7, 5), "7y+": (7, None, 10)}
EvdsCode = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9._-]+$")]


class TurkeyRequest(BaseModel):
    period: Literal["6mo", "1y", "2y", "3y", "5y"] = "1y"
    view: Literal["snapshot", "series"] = Field(
        "snapshot", description="Latest readings and changes, or also the monthly histories."
    )
    evds_codes: list[EvdsCode] | None = Field(
        None, max_length=8, description="Extra CBRT EVDS series to fetch. Requires EVDS_API_KEY."
    )


class CPIObservation(Output):
    date: Day
    yoy: Percent
    mom: Percent


class Inflation(Output):
    latest: CPIObservation
    yoy_change_3m: Points
    mom_change_3m: Points
    observations: Annotated[list[CPIObservation], hint(rows=6)]


class EvdsObservation(Output):
    date: Day
    value: Number


class EvdsReading(EvdsObservation):
    code: Code
    unit: Code
    change_3m: Number
    change_12m: Number
    observations: Annotated[list[EvdsObservation], hint(rows=6)] = []


class Eurobond(Output):
    isin: Code
    maturity: Day
    years_to_maturity: Number
    ask_yield: Percent


class TurkeyOutput(Output):
    inflation: Inflation
    evds: dict[str, EvdsReading]
    real_funding_rate: Percent = None
    real_deposit_rate: Percent = None
    eurobonds: dict[str, Eurobond]
    notes: list[Text] = []


def _date(raw: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _inflation(frame: Any, today: dt.date, series: bool) -> Inflation:
    if not {"YearlyInflation", "MonthlyInflation"} <= set(getattr(frame, "columns", ())):
        raise ToolError("TÜİK returned invalid CPI history.")
    readings = {}
    for stamp, raw_yoy, raw_mom in frame[["YearlyInflation", "MonthlyInflation"]].itertuples(
        name=None
    ):
        date = _date(stamp)
        yoy, mom = bist._finite_number(raw_yoy), bist._finite_number(raw_mom)
        if date and date <= today and yoy is not None and mom is not None:
            readings[date.replace(day=1)] = CPIObservation(date=date.isoformat(), yoy=yoy, mom=mom)
    if not readings:
        raise ToolError("TÜİK returned no usable CPI observations.")
    newest = max(readings)
    latest = readings[newest]
    prior = readings.get(months_back(newest, 3))
    observations = [readings[d] for d in sorted(readings)]
    return Inflation(
        latest=latest,
        yoy_change_3m=latest.yoy - prior.yoy if prior else None,
        mom_change_3m=latest.mom - prior.mom if prior else None,
        observations=observations if series else observations[-4:],
    )


def _evds_readings(
    frame: Any, indicators: dict[str, tuple[str, str]], start: dt.date, today: dt.date, series: bool
) -> dict[str, EvdsReading]:
    columns = {c.replace("_", "."): c for c in getattr(frame, "columns", ())}
    readings = {}
    missing = []
    for name, (code, unit) in indicators.items():
        values = {}
        if code in columns:
            for stamp, raw in frame[columns[code]].items():
                date, value = _date(stamp), bist._finite_number(raw)
                if date and date <= today and value is not None:
                    values[date.replace(day=1)] = value
        in_window = sorted(d for d in values if d >= start)
        if not in_window:
            missing.append(code)
            continue
        latest = in_window[-1]
        value = values[latest]
        prior_3m = values.get(months_back(latest, 3))
        prior_12m = values.get(months_back(latest, 12))
        readings[name] = EvdsReading(
            code=code,
            unit=unit,
            date=latest.isoformat(),
            value=value,
            change_3m=value - prior_3m if prior_3m is not None else None,
            change_12m=value - prior_12m if prior_12m is not None else None,
            observations=[EvdsObservation(date=d.isoformat(), value=values[d]) for d in in_window]
            if series
            else [],
        )
    if missing:
        raise ToolError(
            "CBRT EVDS returned no usable observations in the window for "
            + ", ".join(missing)
            + "."
        )
    return readings


def _eurobonds(frame: Any, today: dt.date) -> dict[str, Eurobond]:
    columns = ["isin", "maturity", "ask_yield"]
    if not set(columns) <= set(getattr(frame, "columns", ())):
        raise ToolError("Ziraat Bank returned invalid USD sovereign Eurobond quotes.")
    bonds = []
    for isin, stamp, raw_yield in frame[columns].itertuples(index=False, name=None):
        maturity, ask_yield = _date(stamp), bist._finite_number(raw_yield)
        if (
            maturity
            and maturity > today
            and ask_yield is not None
            and ask_yield > 0
            and isinstance(isin, str)
            and re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin)
        ):
            bonds.append(
                Eurobond(
                    isin=isin,
                    maturity=maturity.isoformat(),
                    years_to_maturity=(maturity - today).days / 365.25,
                    ask_yield=ask_yield,
                )
            )
    buckets = {}
    for name, (low, high, target) in _BUCKETS.items():
        start = months_back(today, -12 * low).isoformat()
        end = months_back(today, -12 * high).isoformat() if high is not None else None
        candidates = (b for b in bonds if start <= b.maturity and (end is None or b.maturity < end))
        if best := min(
            candidates, key=lambda b: (abs(b.years_to_maturity - target), b.isin), default=None
        ):
            buckets[name] = best
    if not buckets:
        raise ToolError("Ziraat Bank returned no usable USD sovereign Eurobond yields for 1y+.")
    return buckets


async def _handler(req: TurkeyRequest) -> Result:
    today = dt.datetime.now(bist.istanbul()).date()
    series = req.view == "series"
    indicators = dict(_INDICATORS)
    defaults = {code for code, _ in _INDICATORS.values()}
    for code in req.evds_codes or []:
        code = code.replace("_", ".")
        if code not in defaults:
            indicators[code] = (code, "as published")
    cpi_frame, bond_frame, evds_frame = await bist.fetch_turkey_macro(
        req.period,
        True,
        [code for code, _ in indicators.values()],
        require_evds=bool(req.evds_codes),
        as_of=today,
    )
    inflation = _inflation(cpi_frame, today, series)
    eurobonds = _eurobonds(bond_frame, today)
    evds = {}
    notes = [
        "Eurobonds are indicative Ziraat ask yields without quote timestamps, not a risk-free "
        "USD curve. Representative issues are nearest 2y, 5y and 10y within their buckets."
    ]
    if missing := _BUCKETS.keys() - eurobonds.keys():
        notes.append("No usable Eurobond quotes for: " + ", ".join(sorted(missing)) + ".")
    source = "TÜİK CPI (via CBRT), Ziraat Bank (via borsapy)"
    if evds_frame is None:
        notes.append("EVDS_API_KEY is unset; CBRT EVDS metrics were not queried.")
    else:
        months = 6 if req.period == "6mo" else int(req.period[:-1]) * 12
        start = months_back(today.replace(day=1), months - 1)
        evds = _evds_readings(evds_frame, indicators, start, today, series)
        source += ", CBRT EVDS (via borsapy)"
        notes.extend(
            [
                "EVDS uses the last reading in each month, including partial months. Dates label "
                "months; changes are in series units (percentage points for rates).",
                "Real rates subtract the latest CPI YoY from the latest nominal rate. This is an "
                "ex-post proxy, not an inflation forecast or an expected USD return.",
            ]
        )
    yoy = inflation.latest.yoy
    out = TurkeyOutput(
        inflation=inflation,
        evds=evds,
        eurobonds=eurobonds,
        notes=notes,
        real_funding_rate=evds["funding_rate"].value - yoy if evds else None,
        real_deposit_rate=evds["try_deposit_3m"].value - yoy if evds else None,
    )
    latest_dates = [inflation.latest.date, *(row.date for row in evds.values())]
    as_of = max((date for date in latest_dates if date), default=None)
    return Result(
        out, Provenance(source=source, subject="Turkey macro", view=req.view, as_of=as_of)
    )


SPEC = ToolSpec(
    name="data_macro_turkey",
    display_name="data.macro.turkey",
    result_kind="data",
    tier="deferred",
    description=(
        "Turkish macro snapshot: TÜİK CPI, CBRT funding cost, CPI-based real effective exchange "
        "rate, gross FX reserves excluding gold, TRY 3-month deposit and commercial loan rates, "
        "and Turkish USD sovereign Eurobond ask yields. The weighted funding cost is not the "
        "policy rate. Use view='series' for monthly CPI and EVDS history. EVDS needs EVDS_API_KEY; "
        "without it only CPI and Eurobonds are queried. Add up to eight codes with evds_codes."
    ),
    request_schema=TurkeyRequest,
    handler=_handler,
    output=TurkeyOutput,
    tags=("macro", "turkey", "inflation", "rates", "bonds", "data"),
)
