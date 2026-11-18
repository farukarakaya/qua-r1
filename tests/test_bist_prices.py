# SPDX-License-Identifier: Apache-2.0

import datetime as dt
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from trading_r1.contract.spec import ToolError
from trading_r1.tools.data import bist, pricing
from trading_r1.tools.data.market import aligned_prices, prices


class BistPriceTests(unittest.IsolatedAsyncioTestCase):
    async def test_aliases_and_provenance(self) -> None:
        ticker = Mock()
        frame = MagicMock(empty=False)
        frame.__getitem__.return_value.itertuples.return_value = [
            (dt.datetime(2024, 6, day, 21, tzinfo=dt.UTC), 10, 12, 9, 11, 100) for day in (3, 2, 1)
        ]
        ticker.return_value.history.return_value = frame
        with (
            patch.dict(sys.modules, {"borsapy": SimpleNamespace(Ticker=ticker, indices=list)}),
            patch.object(pricing, "Tauric", side_effect=AssertionError("unexpected Tauric call")),
        ):
            for symbol in ("THYAO.IS", "XIST:THYAO"):
                out = await prices.SPEC.handler(
                    prices.MarketPricesRequest(symbol=symbol, start="2024-06-03", end="2024-06-04")
                )
                self.assertEqual(out.output.key, "security:XIST:THYAO")
                self.assertEqual(out.output.dates, ["2024-06-03", "2024-06-04"])
                self.assertEqual(out.output.currency, "TRY")
                self.assertEqual(out.output.price_basis, "split_adjusted")
                self.assertEqual(set(out.output.period_return), {"price"})
                self.assertEqual(out.provenance.source, bist.SOURCE)
        ticker.assert_called_with("THYAO")
        ticker.return_value.history.assert_called_with(
            interval="1d",
            start="2024-06-03",
            end="2024-06-04",
            adjust=True,
            actions=False,
            auto_adjust=False,
        )

    async def test_bist_failures_do_not_call_tauric(self) -> None:
        ticker = Mock(side_effect=RuntimeError("raw provider error"))
        with (
            patch.dict(sys.modules, {"borsapy": SimpleNamespace(Ticker=ticker, indices=list)}),
            patch.object(pricing, "Tauric", side_effect=AssertionError("unexpected Tauric call")),
        ):
            with self.assertRaisesRegex(ToolError, "daily, weekly, and monthly"):
                await pricing.fetch_bars("THYAO.IS", "5d", "15m")
            ticker.assert_not_called()
            with self.assertRaisesRegex(ToolError, "could not fetch history for XIST:THYAO"):
                await pricing.fetch_bars("THYAO.IS", "5d", "1d")

    async def test_index_history_uses_index_not_ticker(self) -> None:
        index = Mock()
        frame = MagicMock(empty=False)
        frame.__getitem__.return_value.itertuples.return_value = [
            (dt.datetime(2024, 6, 3), 100, 105, 95, 102, float("nan")),
        ]
        index.return_value.history.return_value = frame
        bp = SimpleNamespace(Index=index, indices=lambda: ["XU100"])
        with patch.dict(sys.modules, {"borsapy": bp}):
            bars = await pricing.fetch_bars(
                "XIST:XU100", "1d", "1d", dt.date(2024, 6, 3), dt.date(2024, 6, 3)
            )
        index.assert_called_once_with("XU100")
        index.return_value.history.assert_called_once_with(
            interval="1d", start="2024-06-03", end="2024-06-03"
        )
        self.assertEqual((bars.instrument_type, bars.price_basis), ("index", "quoted"))
        self.assertIsNone(bars.rows[0]["volume"])

    async def test_us_history_still_uses_tauric(self) -> None:
        response = {"instrument": {"currency": "USD"}, "price_basis": "split_dividend_adjusted"}
        raw = [{"date": "2024-06-03", "open": 99, "close": 100}]
        with (
            patch.object(pricing, "Tauric"),
            patch.object(pricing, "_daily_pages", AsyncMock(return_value=(response, raw))) as pages,
            patch.object(bist, "fetch_daily", side_effect=AssertionError("unexpected BIST call")),
        ):
            bars = await pricing.fetch_bars("AAPL", "1d", "1d")
        self.assertEqual(pages.await_args.args[1].path, "/securities/AAPL")
        self.assertEqual(
            (bars.source, bars.currency, bars.price_basis),
            ("Tauric", "USD", "split_dividend_adjusted"),
        )

    async def test_mixed_basket_preserves_sources_and_currencies(self) -> None:
        rows = [{"date": "2024-06-03", "close": 100}, {"date": "2024-06-04", "close": 110}]
        with patch.object(aligned_prices, "fetch_bars") as fetch:
            fetch.side_effect = [
                pricing.Bars(rows, currency="USD", price_basis="split_dividend_adjusted"),
                pricing.Bars(
                    rows, currency="TRY", price_basis="split_adjusted", source=bist.SOURCE
                ),
            ]
            req = aligned_prices.AlignedPricesRequest(symbols=["AAPL", "THYAO.IS"])
            result = await aligned_prices.SPEC.handler(req)
        stats = result.payload["per_symbol"]
        self.assertEqual(result.payload["dates"], ["2024-06-03", "2024-06-04"])
        self.assertEqual(stats["AAPL"]["source"], "Tauric")
        self.assertEqual(stats["THYAO.IS"]["source"], bist.SOURCE)
        self.assertEqual(stats["THYAO.IS"]["currency"], "TRY")
        self.assertEqual(stats["THYAO.IS"]["price_basis"], "split_adjusted")

    async def test_bist_baskets_reject_aliases_and_provider_failures(self) -> None:
        with patch.object(aligned_prices, "fetch_bars", new_callable=AsyncMock) as fetch:
            req = aligned_prices.AlignedPricesRequest(symbols=["XIST:THYAO", "THYAO.IS"])
            with self.assertRaisesRegex(ToolError, "only one spelling"):
                await aligned_prices.SPEC.handler(req)
            fetch.assert_not_called()
            req = aligned_prices.AlignedPricesRequest(symbols=["AAPL", "MSFT", "THYAO.IS"])
            fetch.side_effect = [pricing.Bars([]), pricing.Bars([]), ToolError("feed unavailable")]
            with self.assertRaisesRegex(ToolError, "full BIST basket: feed unavailable"):
                await aligned_prices.SPEC.handler(req)
