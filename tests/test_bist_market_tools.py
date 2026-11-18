# SPDX-License-Identifier: Apache-2.0

import datetime as dt
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from trading_r1.contract.spec import ToolError
from trading_r1.runtime.sources import source_batch, source_scope
from trading_r1.tools.data import bist
from trading_r1.tools.data.market import news, quotes, symbol_search


def _tauric() -> MagicMock:
    api = MagicMock(limits=[])
    api.__aenter__.return_value = api
    return api


def _borsapy(**methods: object) -> SimpleNamespace:
    return SimpleNamespace(indices=lambda: ["XU100"], **methods)


class BistMarketTests(unittest.IsolatedAsyncioTestCase):
    async def test_quotes_deduplicate_bist_aliases_without_tauric(self) -> None:
        ticker = Mock(
            return_value=SimpleNamespace(
                info={
                    "last": 321.5,
                    "prev_close": 320,
                    "change": 1.5,
                    "change_percent": 0.46875,
                    "low": 319,
                    "high": 322,
                    "volume": 100,
                    "currency": "TRY",
                    "timestamp": None,
                }
            )
        )
        with (
            patch.dict(sys.modules, {"borsapy": _borsapy(Ticker=ticker)}),
            patch.object(quotes, "Tauric", side_effect=AssertionError("unexpected Tauric call")),
        ):
            req = quotes.MarketQuotesRequest(symbols=["thyao.is", "security:XIST:THYAO"])
            result = await quotes.SPEC.handler(req)
        ticker.assert_called_once_with("THYAO")
        self.assertEqual(list(result.output.quotes), ["XIST:THYAO"])
        quote = result.output.quotes["XIST:THYAO"]
        self.assertEqual((quote.price, quote.currency, quote.previous_close), (321.5, "TRY", 320))
        self.assertEqual((quote.change, quote.change_pct, quote.volume), (1.5, 0.46875, 100))
        self.assertEqual((quote.day_low, quote.day_high), (319, 322))
        self.assertIsNone(quote.as_of)
        self.assertIsNone(quote.session_date)
        self.assertIsNone(result.provenance.as_of)
        self.assertEqual(result.provenance.source, bist.SOURCE)

    async def test_bist_index_uses_index_info_and_does_not_assume_currency(self) -> None:
        index = Mock(return_value=SimpleNamespace(info={"last": 10000, "prev_close": 9900}))
        with (
            patch.dict(sys.modules, {"borsapy": _borsapy(Index=index)}),
            patch.object(quotes, "Tauric", side_effect=AssertionError("unexpected Tauric call")),
        ):
            result = await quotes.SPEC.handler(quotes.MarketQuotesRequest(symbols=["XIST:XU100"]))
        index.assert_called_once_with("XU100")
        quote = result.output.quotes["XIST:XU100"]
        self.assertEqual((quote.price, quote.previous_close), (10000, 9900))
        self.assertIsNone(quote.currency)

    async def test_mixed_quotes_batch_only_non_bist_and_isolate_bist_failures(self) -> None:
        stamp = dt.datetime(2024, 6, 3, 9, 8, 7, tzinfo=dt.UTC).timestamp()

        def ticker(symbol: str) -> SimpleNamespace:
            if symbol == "GARAN":
                raise RuntimeError("private provider details")
            return SimpleNamespace(info={"last": 50, "timestamp": stamp})

        api = _tauric()
        api.post = AsyncMock(
            return_value={
                "quotes": {"security:AAPL": {"price": 250, "session_date": "2024-06-02"}},
                "errors": {"crypto:BTC-USDT": {"message": "not covered"}},
            }
        )
        api.limits = ["AAPL: delayed"]
        with (
            patch.dict(sys.modules, {"borsapy": _borsapy(Ticker=ticker)}),
            patch.object(quotes, "Tauric", return_value=api),
        ):
            req = quotes.MarketQuotesRequest(symbols=["XIST:THYAO", "AAPL", "GARAN.IS", "BTC-USD"])
            result = await quotes.SPEC.handler(req)
        api.post.assert_awaited_once_with(
            "/quotes", {"instruments": ["security:AAPL", "crypto:BTC-USDT"]}
        )
        self.assertEqual(list(result.output.quotes), ["XIST:THYAO", "AAPL"])
        self.assertEqual(result.output.quotes["XIST:THYAO"].as_of, "2024-06-03T09:08:07Z")
        self.assertEqual(result.output.left_out["BTC-USD"], "not covered")
        self.assertIn("XIST:GARAN", result.output.left_out["GARAN.IS"])
        self.assertNotIn("private provider details", result.output.left_out["GARAN.IS"])
        self.assertIn("AAPL: delayed", result.output.notes)
        self.assertEqual(result.provenance.as_of, "2024-06-03")
        self.assertIn("Tauric", result.provenance.source)
        self.assertIn(bist.SOURCE, result.provenance.source)

    async def test_tauric_outage_does_not_prevent_bist_quotes(self) -> None:
        ticker = Mock(return_value=SimpleNamespace(info={"last": 50}))
        with (
            patch.dict(sys.modules, {"borsapy": _borsapy(Ticker=ticker)}),
            patch.object(quotes, "Tauric", side_effect=ToolError("No Tauric credentials")),
        ):
            result = await quotes.SPEC.handler(
                quotes.MarketQuotesRequest(symbols=["THYAO.IS", "AAPL"])
            )
        self.assertEqual(list(result.output.quotes), ["XIST:THYAO"])
        self.assertEqual(result.output.left_out, {"AAPL": "No Tauric credentials"})
        self.assertEqual(result.provenance.source, bist.SOURCE)

    async def test_news_uses_utc_dates_and_registers_kap_links(self) -> None:
        dates = [
            "10.06.2024 03:00:00",
            "10.06.2024 02:59:59",
            "11.06.2024 03:00:00",
            "11.06.2024 02:59:59",
            "invalid",
            "10.06.2024 19:00:00",
        ]
        url = "https://www.kap.org.tr/tr/Bildirim/"
        frame = Mock()
        frame.to_dict.return_value = [
            {
                "Date": date,
                "Title": f"Pay alımı {i}" if i != 5 else "Finansal rapor",
                "URL": url + str(i),
            }
            for i, date in enumerate(dates)
        ]
        frame.to_dict.return_value.extend(
            [
                frame.to_dict.return_value[0].copy(),
                {"Date": dates[0], "Title": "Pay alımı", "URL": "https://example.org/9"},
            ]
        )
        ticker = Mock(return_value=SimpleNamespace(news=frame))
        bibliography = Mock(add_citation=Mock(side_effect=[12, 13]))
        with (
            patch.dict(sys.modules, {"borsapy": SimpleNamespace(Ticker=ticker)}),
            patch.object(news, "Tauric", side_effect=AssertionError("unexpected Tauric call")),
            source_scope(bibliography),
            source_batch() as cited,
        ):
            req = news.NewsRequest(
                symbol="THYAO.IS", start="2024-06-10", end="2024-06-10", query="pay"
            )
            result = await news.SPEC.handler(req)
        ticker.assert_called_once_with("THYAO")
        frame.to_dict.assert_called_once_with("records")
        items = result.output.items
        self.assertEqual(result.output.symbol, "XIST:THYAO")
        self.assertEqual(
            [x.published for x in items], ["2024-06-10T23:59:59Z", "2024-06-10T00:00:00Z"]
        )
        self.assertEqual([x.time_basis for x in items], ["published", "published"])
        self.assertEqual([x.publisher for x in items], ["KAP", "KAP"])
        self.assertEqual([x.source for x in items], [12, 13])
        self.assertEqual([x.url for x in items], [url + "3", url + "0"])
        self.assertEqual(
            [c.args[0] for c in bibliography.add_citation.call_args_list], [x.url for x in items]
        )
        self.assertEqual(cited, [(bibliography, 12), (bibliography, 13)])
        self.assertEqual(result.provenance.as_of, "2024-06-10")

    async def test_bist_news_rejects_sentiment_before_fetching(self) -> None:
        ticker = Mock(side_effect=AssertionError("unexpected BIST call"))
        with (
            patch.dict(sys.modules, {"borsapy": SimpleNamespace(Ticker=ticker)}),
            patch.object(news, "Tauric", side_effect=AssertionError("unexpected Tauric call")),
            self.assertRaisesRegex(ToolError, "sentiment filter"),
        ):
            await news.SPEC.handler(news.NewsRequest(symbol="XIST:THYAO", sentiment="neutral"))
        ticker.assert_not_called()

    async def test_explicit_bist_search_matches_kap_stock_names_and_tickers(self) -> None:
        companies = Mock()
        companies.return_value.to_dict.return_value = [
            {"ticker": "THYAO", "name": "TÜRK HAVA YOLLARI A.O.", "city": "İSTANBUL"},
            {"ticker": "THYAO", "name": "TÜRK HAVA YOLLARI A.O.", "city": "İSTANBUL"},
        ]
        with (
            patch.dict(sys.modules, {"borsapy": SimpleNamespace(search_companies=companies)}),
            patch.object(
                symbol_search, "Tauric", side_effect=AssertionError("unexpected Tauric call")
            ),
        ):
            for alias, expected in [("xist:Türk Hava", "Türk Hava"), ("THY.is", "THY")]:
                result = await symbol_search.SPEC.handler(
                    symbol_search.SymbolSearchRequest(query=alias)
                )
                companies.assert_called_with(expected)
                (match,) = result.output.matches
                self.assertEqual(match.symbol, "XIST:THYAO")
                self.assertEqual(match.key, "security:XIST:THYAO")
                self.assertEqual(
                    (match.name, match.instrument_type), ("TÜRK HAVA YOLLARI A.O.", "stock")
                )
                self.assertIn("KAP", result.provenance.source)

    async def test_explicit_bist_index_search_is_filtered_by_exchange(self) -> None:
        search = Mock(
            return_value=[
                {"symbol": "XU100", "description": "Foreign match", "exchange": "CBOE"},
                {"symbol": "XU099", "description": "Unsupported BIST index", "exchange": "BIST"},
                {"symbol": "XU100", "description": "BIST 100", "exchange": "BIST"},
            ]
        )
        with (
            patch.dict(sys.modules, {"borsapy": _borsapy(search=search)}),
            patch.object(
                symbol_search, "Tauric", side_effect=AssertionError("unexpected Tauric call")
            ),
        ):
            req = symbol_search.SymbolSearchRequest(query="XIST:XU", asset_type="index")
            result = await symbol_search.SPEC.handler(req)
        search.assert_called_once_with(
            "XU", type="index", exchange="BIST", limit=req.limit, full_info=True
        )
        (match,) = result.output.matches
        self.assertEqual(
            (match.symbol, match.name, match.instrument_type), ("XIST:XU100", "BIST 100", "index")
        )
        self.assertEqual(result.provenance.source, bist.SOURCE)

    async def test_missing_borsapy_never_falls_back_to_tauric(self) -> None:
        cases = [
            (quotes, quotes.MarketQuotesRequest(symbols=["THYAO.IS"])),
            (news, news.NewsRequest(symbol="THYAO.IS")),
            (symbol_search, symbol_search.SymbolSearchRequest(query="THYAO.IS")),
        ]
        for tool, req in cases:
            with (
                self.subTest(tool=tool.SPEC.name),
                patch.dict(sys.modules, {"borsapy": None}),
                patch.object(tool, "Tauric", side_effect=AssertionError("unexpected Tauric call")),
                self.assertRaisesRegex(ToolError, "'bist' extra"),
            ):
                await tool.SPEC.handler(req)

    async def test_unqualified_symbols_and_searches_still_use_tauric(self) -> None:
        key = "security:THYAO"
        api = _tauric()
        api.post = AsyncMock(return_value={"quotes": {key: {"price": 100}}})
        api.get = AsyncMock(
            side_effect=[
                {"data": [{"title": "Tauric article", "id": 42}]},
                {"data": [{"key": key, "name": "Tauric listing"}]},
            ]
        )
        with (
            patch.object(bist, "run", side_effect=AssertionError("unexpected BIST call")),
            patch.object(quotes, "Tauric", return_value=api),
            patch.object(news, "Tauric", return_value=api),
            patch.object(symbol_search, "Tauric", return_value=api),
        ):
            q = await quotes.SPEC.handler(quotes.MarketQuotesRequest(symbols=["THYAO"]))
            n = await news.SPEC.handler(news.NewsRequest(symbol="THYAO", sentiment="neutral"))
            s = await symbol_search.SPEC.handler(symbol_search.SymbolSearchRequest(query="THYAO"))
        api.post.assert_awaited_once_with("/quotes", {"instruments": [key]})
        self.assertEqual(api.get.await_args_list[0].args, ("/news",))
        self.assertEqual(api.get.await_args_list[0].kwargs["instrument"], key)
        self.assertEqual(api.get.await_args_list[0].kwargs["sentiment"], "neutral")
        api.get.assert_awaited_with("/instruments", q="THYAO", limit=8)
        self.assertEqual(q.output.quotes["THYAO"].price, 100)
        self.assertEqual(n.output.items[0].title, "Tauric article")
        self.assertEqual(s.output.matches[0].symbol, "THYAO")
