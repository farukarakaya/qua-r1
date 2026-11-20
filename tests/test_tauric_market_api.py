# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import datetime as dt
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pandas as pd

from trading_r1.contract.spec import ToolError
from trading_r1.tools.compute.risk.drawdown import SPEC as DRAWDOWN
from trading_r1.tools.compute.risk.drawdown import DrawdownRequest
from trading_r1.tools.data import bist, pricing
from trading_r1.tools.data.market import aligned_prices, prices, quotes
from trading_r1.tools.data.tauric import stated_limits


class TauricMarketApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_quotes_parses_list_data_list_errors_and_dict_change(self) -> None:
        api = AsyncMock()
        api.__aenter__.return_value = api
        api.__aexit__.return_value = False
        api.limits = []
        api.post = AsyncMock(
            return_value={
                "object": "list",
                "data": [
                    {
                        "key": "security:AAPL",
                        "instrument": {"key": "security:AAPL", "currency": "USD"},
                        "price": 349.31,
                        "price_time": "2026-11-19T20:57:21.000000Z",
                        "session_date": "2026-11-19",
                        "previous_close": {"value": 352.45},
                        "change": {"absolute": -3.14, "percent": -0.8909},
                        "day": {"high": 355.28, "low": 346.47, "volume": 29264334},
                        "range_52w": {"high": 360.83, "low": 243.42},
                        "market_state": "regular",
                    }
                ],
                "errors": [
                    {
                        "key": "fx:USDBRL",
                        "error": {
                            "code": "not_supported",
                            "message": "FX pair `USDBRL` is not served: outside the FX coverage",
                        },
                    }
                ],
            }
        )

        class FakeBorsapy:
            def indices(self):
                return ["XU100"]

            def Ticker(self, symbol: str):
                return SimpleNamespace(info={"last": 310.5, "currency": "TRY"})

        with (
            patch.object(quotes, "Tauric", return_value=api),
            patch.dict("sys.modules", {"borsapy": FakeBorsapy()}),
        ):
            result = await quotes.SPEC.handler(
                quotes.MarketQuotesRequest(symbols=["THYAO.IS", "AAPL", "USDBRL=X"])
            )

        self.assertEqual(list(result.output.quotes), ["XIST:THYAO", "AAPL"])
        aapl = result.output.quotes["AAPL"]
        self.assertEqual(aapl.price, 349.31)
        self.assertEqual(aapl.change, -3.14)
        self.assertEqual(aapl.change_pct, -0.8909)
        self.assertEqual(aapl.as_of, "2026-11-19T20:57:21.000000Z")
        self.assertEqual(
            result.output.left_out,
            {"USDBRL=X": "FX pair `USDBRL` is not served: outside the FX coverage"},
        )

    async def test_quotes_only_unsupported_fx_raises_tool_error_not_attribute_error(self) -> None:
        api = AsyncMock()
        api.__aenter__.return_value = api
        api.__aexit__.return_value = False
        api.limits = []
        api.post = AsyncMock(
            return_value={
                "object": "list",
                "data": [],
                "errors": [
                    {
                        "key": "fx:USDBRL",
                        "error": {
                            "code": "not_supported",
                            "message": "FX pair `USDBRL` is not served: outside the FX coverage",
                        },
                    }
                ],
            }
        )
        with (
            patch.object(quotes, "Tauric", return_value=api),
            self.assertRaisesRegex(ToolError, "outside the FX coverage"),
        ):
            await quotes.SPEC.handler(quotes.MarketQuotesRequest(symbols=["USDBRL=X"]))

    async def test_daily_history_uses_start_end_pagination_and_data_rows(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def fake_get(path: str, **params):
            calls.append((path, params))
            self.assertNotIn("period", params)
            start = params["start"]
            end = params["end"]
            return {
                "instrument": {"key": "fx:EURUSD", "currency": "USD"},
                "price_basis": "unadjusted",
                "coverage": {"continue_end": None},
                "data": [
                    {"date": start, "open": 1.10, "high": 1.11, "low": 1.09, "close": None},
                    {"date": end, "open": 1.12, "high": 1.13, "low": 1.11, "close": None},
                ],
            }

        api = AsyncMock()
        api.__aenter__.return_value = api
        api.__aexit__.return_value = False
        api.limits = []
        api.get = AsyncMock(side_effect=fake_get)

        with patch.object(pricing, "Tauric", return_value=api):
            bars = await pricing.fetch_bars(
                "EURUSD=X",
                period="1y",
                interval="1d",
                start=dt.date(2025, 11, 18),
                end=dt.date(2026, 11, 18),
            )

        self.assertGreaterEqual(len(calls), 2)
        for path, params in calls:
            self.assertEqual(path, "/fx/EURUSD/history")
            span = (
                dt.date.fromisoformat(params["end"]) - dt.date.fromisoformat(params["start"])
            ).days
            self.assertLessEqual(span, 182)
        self.assertEqual(bars.rows[0]["date"], "2025-11-18")
        self.assertEqual(bars.rows[-1]["date"], "2026-11-18")

    def test_stated_limits_tolerates_field_states_without_state_key(self) -> None:
        body = {
            "instrument": {"key": "fx:EURUSD"},
            "field_states": {"close": {"reason": "withheld"}, "open": "ok"},
        }
        self.assertEqual(stated_limits(body), [])

    def test_try_fx_base_matches_supported_pairs_only(self) -> None:
        for symbol, expected in (
            ("USDTRY=X", "USD"),
            ("TRY=X", "USD"),
            ("USDTRY", "USD"),
            ("usd/try", "USD"),
            ("fx:EURTRY", "EUR"),
            ("GBP-TRY", "GBP"),
            ("CHFTRY=X", "CHF"),
            ("JPYTRY=X", None),
            ("EURUSD=X", None),
            ("THYAO.IS", None),
        ):
            self.assertEqual(bist.try_fx_base(symbol), expected, symbol)

    async def test_try_fx_quotes_and_aligned_prices_route_through_borsapy(self) -> None:
        fx_frame = pd.DataFrame(
            {
                "Open": [50.1, 50.2, 50.4],
                "High": [50.3, 50.5, 50.6],
                "Low": [50.0, 0.0, 50.1],  # The feed occasionally publishes a broken OHLC bar.
                "Close": [50.2, 100.4, 50.2],  # Retain the FX spike in USD risk calculations.
            },
            index=pd.to_datetime(["2026-11-16 03:00", "2026-11-17 03:00", "2026-11-18 03:00"]),
        )
        stock_frame = pd.DataFrame(
            {
                "Open": [320.0, 322.0, 325.0],
                "High": [324.0, 326.0, 330.0],
                "Low": [319.0, 321.0, 324.0],
                "Close": [322.0, 322.0, 325.0],
                "Volume": [1_000_000, 1_100_000, 1_200_000],
            },
            index=pd.to_datetime(["2026-11-16 03:00", "2026-11-17 03:00", "2026-11-18 03:00"]),
        )

        class FakeBorsapy:
            def indices(self):
                return ["XU100"]

            def Ticker(self, symbol: str):
                return SimpleNamespace(
                    info={"last": 328.0, "currency": "TRY"},
                    history=lambda **kwargs: stock_frame,
                )

            def FX(self, base: str):
                self.last_fx = base
                return SimpleNamespace(
                    current={
                        "symbol": base,
                        "last": 50.4651,
                        "high": 50.61,
                        "low": 50.42,
                        "update_time": dt.datetime(2026, 11, 19, 3, 0),
                    },
                    history=lambda **kwargs: fx_frame,
                )

        fake = FakeBorsapy()
        with (
            patch.object(quotes, "Tauric", side_effect=AssertionError("unexpected Tauric call")),
            patch.object(pricing, "Tauric", side_effect=AssertionError("unexpected Tauric call")),
            patch.dict("sys.modules", {"borsapy": fake}),
        ):
            q = await quotes.SPEC.handler(
                quotes.MarketQuotesRequest(symbols=["USDTRY=X", "TRY=X", "THYAO.IS"])
            )
            p = await prices.SPEC.handler(
                prices.MarketPricesRequest(
                    symbol="USDTRY=X", start=dt.date(2026, 11, 16), end=dt.date(2026, 11, 18)
                )
            )
            aligned = await aligned_prices.SPEC.handler(
                aligned_prices.AlignedPricesRequest(
                    symbols=["THYAO.IS", "USDTRY=X"],
                    start=dt.date(2026, 11, 16),
                    end=dt.date(2026, 11, 18),
                )
            )
            with self.assertRaisesRegex(ToolError, "daily, weekly, and monthly bars only"):
                await pricing.fetch_bars("USDTRY=X", period="5d", interval="15m")

        self.assertEqual(list(q.output.quotes), ["USDTRY", "XIST:THYAO"])
        self.assertEqual(q.output.quotes["USDTRY"].price, 50.4651)
        self.assertEqual(q.output.quotes["USDTRY"].currency, "TRY")
        self.assertEqual(p.output.currency, "TRY")
        self.assertEqual(p.output.current, 50.2)
        self.assertEqual(aligned.output.dates, ["2026-11-16", "2026-11-17", "2026-11-18"])
        try_closes = aligned.output.series["THYAO.IS"]
        fx_closes = aligned.output.series["USDTRY=X"]
        self.assertEqual(fx_closes, [50.2, 100.4, 50.2])
        risk = await DRAWDOWN.handler(
            DrawdownRequest(
                closes=[stock / fx for stock, fx in zip(try_closes, fx_closes, strict=True)],
                dates=aligned.output.dates,
            )
        )
        self.assertAlmostEqual(risk.payload["max_drawdown"], -0.5)
        self.assertEqual(aligned.output.per_symbol["USDTRY=X"].source, bist.FX_SOURCE)
        self.assertEqual(aligned.output.per_symbol["THYAO.IS"].dropped, 0)
        note = "USDTRY kept FX closes for 1 bar(s) without usable open/high/low data: 2026-11-17."
        self.assertIn(note, p.output.notes)
        self.assertIn(note, aligned.output.notes)
        bar = p.payload["bars"][1]
        self.assertEqual(bar["close"], 100.4)
        self.assertTrue(all(bar[k] is None for k in ("open", "high", "low")))
        (weekly,) = pricing.resample(p.payload["bars"], "1wk", complete_extrema=True)
        self.assertEqual(weekly["close"], 50.2)
        self.assertIsNone(weekly["high"])
        self.assertIsNone(weekly["low"])
        with patch.dict("sys.modules", {"borsapy": fake}):
            fx_frame.loc["2026-11-17 03:00", "Close"] = float("nan")
            with self.assertRaisesRegex(ToolError, "invalid USDTRY close on 2026-11-17"):
                await aligned_prices.SPEC.handler(
                    aligned_prices.AlignedPricesRequest(
                        symbols=["THYAO.IS", "USDTRY=X"],
                        start=dt.date(2026, 11, 16),
                        end=dt.date(2026, 11, 18),
                    )
                )
