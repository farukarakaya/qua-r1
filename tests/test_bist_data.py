# SPDX-License-Identifier: Apache-2.0

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from trading_r1.contract.spec import ToolError
from trading_r1.tools.data.equity import EQUITY_DATA_SPECS, fundamentals
from trading_r1.tools.data.market import MARKET_DATA_SPECS, bist_data


class BistDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_financial_statements_keep_native_line_items(self) -> None:
        frame = Mock()
        frame.index.name = "Item"
        frame.reset_index.return_value.to_json.return_value = (
            '[{"Item":"Toplam Varlıklar","2025Q3":1200,"2024Q3":null}]'
        )
        stock = Mock()
        stock.get_balance_sheet.return_value = frame
        with patch.dict(sys.modules, {"borsapy": SimpleNamespace(Ticker=Mock(return_value=stock))}):
            result = await bist_data.SPEC.handler(
                bist_data.BistDataRequest(
                    symbol="AKBNK.IS",
                    dataset="balance_sheet",
                    cadence="quarterly",
                    financial_group="UFRS",
                    periods=3,
                )
            )
        stock.get_balance_sheet.assert_called_once_with(
            quarterly=True, financial_group="UFRS", last_n=3
        )
        frame.reset_index.assert_called_once()
        row = result.payload["rows"][0]
        self.assertEqual(row, {"Item": "Toplam Varlıklar", "2025Q3": 1200, "2024Q3": None})
        self.assertIn("Toplam Varlıklar", result.text)
        self.assertEqual(result.provenance.source, "İş Yatırım via borsapy")

    async def test_capital_changes_preserve_paid_rights(self) -> None:
        stock = SimpleNamespace(splits=[{"Date": "2024-06-03", "RightsIssue": 50}])
        with patch.dict(sys.modules, {"borsapy": SimpleNamespace(Ticker=Mock(return_value=stock))}):
            result = await bist_data.SPEC.handler(
                bist_data.BistDataRequest(symbol="XIST:THYAO", dataset="capital_changes")
            )
        self.assertEqual(result.payload["rows"][0]["RightsIssue"], 50)
        self.assertIn("not split multipliers", result.text)

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

    async def test_bist_extra_is_optional_and_missing_dependency_is_reported(self) -> None:
        with (
            patch.dict(sys.modules, {"borsapy": None}),
            self.assertRaisesRegex(ToolError, "--extra bist"),
        ):
            await bist_data.SPEC.handler(bist_data.BistDataRequest(dataset="companies"))

    async def test_qualified_stock_never_reaches_tauric_equity_tools(self) -> None:
        self.assertIn(bist_data.SPEC, MARKET_DATA_SPECS)
        cases = {
            "data_equity_comps": {"peers": ["AAPL"]},
        }
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
