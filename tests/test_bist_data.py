# SPDX-License-Identifier: Apache-2.0

import datetime as dt
import json
import sys
import unittest
from importlib.util import find_spec
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from trading_r1.contract.spec import ToolError
from trading_r1.tools.data.equity import EQUITY_DATA_SPECS, fundamentals
from trading_r1.tools.data.market import bist_data


@unittest.skipUnless(find_spec("borsapy"), "requires the bist extra")
class BistProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_financial_formats_do_not_share_cached_rows(self) -> None:
        from borsapy._providers.isyatirim import IsYatirimProvider
        from borsapy.cache import Cache

        def respond(request: httpx.Request) -> httpx.Response:
            params = request.url.params
            slots = [(params.get(f"year{i}"), params.get(f"period{i}")) for i in range(1, 5)]
            if slots != [("2025", "9"), ("2025", "6"), ("2025", "6"), ("2025", "6")]:
                return httpx.Response(200, json={"ok": False, "errorCode": "SYS0000"})
            group = params["financialGroup"]
            total = {"XI_29": 1200, "UFRS": 2400}[group]
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"itemCode": "1000", "itemDescTr": "Toplam Varlıklar", "value1": total},
                        {"itemCode": "1100", "itemDescTr": "Nakit", "value2": 50},
                        {"itemCode": "3000", "itemDescTr": "Gelir", "value1": 80},
                    ]
                },
            )

        with (
            IsYatirimProvider() as provider,
            httpx.Client(transport=httpx.MockTransport(respond)) as client,
            patch.object(provider, "_client", client),
            patch.object(provider, "_cache", Cache()),
            patch.object(provider, "_get_periods", return_value=[(2025, 9), (2025, 6)]),
            patch("borsapy._providers.isyatirim.get_isyatirim_provider", return_value=provider),
        ):
            for group, total in (("XI_29", 1200), ("UFRS", 2400), ("XI_29", 1200)):
                with self.subTest(group=group):
                    result = await bist_data.SPEC.handler(
                        bist_data.BistDataRequest(
                            symbol="AKBNK.IS",
                            dataset="balance_sheet",
                            cadence="quarterly",
                            financial_group=group,
                            periods=2,
                        )
                    )
                    self.assertEqual(
                        result.payload["rows"],
                        [
                            {"Item": "Toplam Varlıklar", "2025Q3": total, "2025Q2": None},
                            {"Item": "Nakit", "2025Q3": None, "2025Q2": 50},
                        ],
                    )

    async def test_corporate_actions_use_istanbul_dates_on_a_utc_host(self) -> None:
        from borsapy._providers.isyatirim import IsYatirimProvider
        from borsapy.cache import Cache

        class UTCClock(dt.datetime):
            @classmethod
            def fromtimestamp(cls, timestamp: float, tz: dt.tzinfo | None = None) -> dt.datetime:
                result = super().fromtimestamp(timestamp, tz or dt.UTC)
                return result if tz else result.replace(tzinfo=None)

        # 2024-06-02 21:00 UTC is midnight on the next day in Istanbul.
        stamp = int(dt.datetime(2024, 6, 2, 21, tzinfo=dt.UTC).timestamp() * 1000)
        raw = {
            "d": json.dumps(
                [
                    {
                        "SHT_KODU": "03",
                        "SHHE_TARIH": stamp,
                        "SHHE_BDLI_ORAN": 50,
                        "SHHE_BDSZ_IK_ORAN": 25,
                    },
                    {"SHT_KODU": "04", "SHHE_TARIH": stamp, "SHHE_NAKIT_TM_ORAN": 225},
                ]
            )
        }
        with (
            IsYatirimProvider() as provider,
            patch.object(provider, "_cache", Cache()),
            patch.object(provider, "_fetch_sermaye_data", return_value=raw),
            patch("borsapy._providers.isyatirim.get_isyatirim_provider", return_value=provider),
            patch("borsapy._providers.isyatirim.datetime", UTCClock),
        ):
            actions = await bist_data.SPEC.handler(
                bist_data.BistDataRequest(symbol="XIST:THYAO", dataset="capital_changes")
            )
            dividends = await bist_data.SPEC.handler(
                bist_data.BistDataRequest(symbol="THYAO.IS", dataset="dividends")
            )
        (action,) = actions.payload["rows"]
        (dividend,) = dividends.payload["rows"]
        self.assertEqual(action["Date"][:10], "2024-06-03")
        self.assertEqual((action["RightsIssue"], action["BonusFromCapital"]), (50, 25))
        self.assertEqual((dividend["Date"][:10], dividend["Amount"]), ("2024-06-03", 2.25))

    async def test_disclosure_uses_id_without_a_ticker(self) -> None:
        content = Mock(return_value="<header>Site menu</header><main><p>Pay alımı</p></main>")
        with (
            patch("borsapy.Ticker", side_effect=AssertionError("not a ticker lookup")),
            patch(
                "borsapy._providers.kap.get_kap_provider",
                return_value=SimpleNamespace(get_disclosure_content=content),
            ),
        ):
            result = await bist_data.SPEC.handler(
                bist_data.BistDataRequest(dataset="disclosure", disclosure_id=1530826)
            )
        content.assert_called_once_with(1530826)
        self.assertIsNone(result.output.symbol)
        self.assertEqual(result.provenance.subject, "KAP 1530826")
        self.assertEqual(result.payload["rows"][0]["text"], "Pay alımı")


class BistDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_missing_and_irrelevant_inputs_before_fetching(self) -> None:
        cases = [
            ({"dataset": "screener", "symbol": "XIST:THYAO"}, "does not support: symbol"),
            (
                {"dataset": "dividends", "symbol": "THYAO.IS", "periods": 2},
                "does not support: periods",
            ),
            ({"dataset": "profile"}, "qualified BIST symbol"),
            ({"dataset": "disclosure"}, "Provide a numeric KAP disclosure_id"),
        ]
        with patch.object(bist_data.bist, "run") as fetch:
            for args, error in cases:
                with self.subTest(args=args), self.assertRaisesRegex(ToolError, error):
                    await bist_data.SPEC.handler(bist_data.BistDataRequest(**args))
        fetch.assert_not_called()

    async def test_screener_does_not_drop_failed_index_filter(self) -> None:
        bp = SimpleNamespace(
            Index=Mock(return_value=SimpleNamespace(component_symbols=[])), screen_stocks=Mock()
        )
        with (
            patch.dict(sys.modules, {"borsapy": bp}),
            self.assertRaisesRegex(ToolError, "No BIST index constituents for XU100"),
        ):
            await bist_data.SPEC.handler(
                bist_data.BistDataRequest(dataset="screener", index="XIST:XU100")
            )
        bp.screen_stocks.assert_not_called()

    async def test_qualified_stock_never_reaches_tauric_equity_tools(self) -> None:
        cases = {"data_equity_comps": {"peers": ["AAPL"]}}
        for spec in EQUITY_DATA_SPECS:
            with self.subTest(tool=spec.name):
                args = {"symbol": "XIST:THYAO", **cases.get(spec.name, {})}
                with self.assertRaisesRegex(ToolError, "does not serve BIST"):
                    await spec.handler(spec.request_schema(**args))
        comp = next(s for s in EQUITY_DATA_SPECS if s.name == "data_equity_comps")
        with self.assertRaisesRegex(ToolError, "does not serve BIST"):
            await comp.handler(comp.request_schema(symbol="AAPL", peers=["THYAO.IS"]))

    async def test_legacy_fundamentals_still_calls_tauric_for_aapl(self) -> None:
        spec = next(s for s in EQUITY_DATA_SPECS if s.name == fundamentals.SPEC.name)
        with (
            patch.object(fundamentals, "Tauric", side_effect=ToolError("Tauric fixture")) as api,
            self.assertRaisesRegex(ToolError, "Tauric fixture"),
        ):
            await spec.handler(spec.request_schema(symbol="AAPL"))
        api.assert_called_once()
