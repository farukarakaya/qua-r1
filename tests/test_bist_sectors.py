# SPDX-License-Identifier: Apache-2.0

import datetime as dt
import unittest
from unittest.mock import patch

import borsapy as bp
import pandas as pd
from pydantic import ValidationError

from trading_r1.contract.spec import ToolError
from trading_r1.tools.data import bist
from trading_r1.tools.data.market import bist_sectors as tool
from trading_r1.tools.data.pricing import Bars

_DATES = ["2025-01-03", "2025-01-06", "2025-01-10", "2025-01-13", "2025-01-17"]


class BistSectorsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fx = [10, 12, 11, 13, 12]
        self.paths = {
            "XIST:XBANK": [fx * 10 for fx in self.fx],
            "XIST:XUTEK": [120] * len(self.fx),
            tool.BENCHMARK: [10 * 10, 12 * 11, 11 * 7, 13 * 9, 12 * 9],
            tool.FX: self.fx,
        }
        self.fetched = self.enterContext(patch.object(tool, "fetch_bars", side_effect=self.bars))

    async def bars(self, symbol, period, interval, *_):
        self.assertEqual(interval, "1d")
        if symbol not in self.paths:
            raise ToolError("Index history unavailable.")
        rows = [{"date": d, "close": v} for d, v in zip(_DATES, self.paths[symbol], strict=True)]
        if symbol == tool.FX:
            rows.insert(1, {"date": "2025-01-04", "close": 500})  # not a BIST session
        return Bars(rows, source="mock provider")

    async def test_sector_ranking_and_risk_use_shared_usd_window(self):
        result = await tool.SPEC.handler(tool.BistSectorsRequest(sectors=["xuTEK", "XIST:XBANK"]))
        out = result.payload
        self.assertEqual(out["n_bars"], len(_DATES))
        self.assertEqual(out["n_fx_returns"], len(_DATES) - 1)
        self.assertEqual((out["start"], out["end"]), (_DATES[0], _DATES[-1]))
        self.assertEqual(self.fetched.await_count, 4)
        bank, tech = out["sectors"]
        self.assertEqual(bank["symbol"], "XIST:XBANK")
        self.assertAlmostEqual(bank["try_return"], 0.2)
        self.assertAlmostEqual(bank["usd_return"], 0)
        self.assertAlmostEqual(bank["excess_usd_return"], 0.1)
        self.assertAlmostEqual(bank["usd_volatility"], 0)
        self.assertAlmostEqual(bank["usd_max_drawdown"], 0)
        self.assertAlmostEqual(bank["fx_beta"], 1)
        self.assertAlmostEqual(tech["try_return"], 0)
        self.assertAlmostEqual(tech["usd_return"], 10 / 12 - 1)
        self.assertAlmostEqual(tech["excess_usd_return"], 10 / 12 - 0.9)
        self.assertAlmostEqual(tech["usd_max_drawdown"], 10 / 13 - 1)
        self.assertAlmostEqual(tech["fx_beta"], 0)
        benchmark = out["benchmark"]
        self.assertAlmostEqual(benchmark["usd_return"], -0.1)
        self.assertAlmostEqual(benchmark["excess_usd_return"], 0)
        self.assertAlmostEqual(benchmark["usd_max_drawdown"], 7 / 11 - 1)
        self.assertIn("excluding cash dividends", result.text)

    async def test_weekly_metrics_sample_the_join_but_beta_is_daily(self):
        original = self.bars

        async def fewer_bars(symbol, *args):
            bars = await original(symbol, *args)
            if symbol == "XIST:XBANK":
                bars.rows[:] = [r for r in bars.rows if r["date"] != "2025-01-10"]
            return bars

        self.fetched.side_effect = fewer_bars
        out = (
            await tool.SPEC.handler(
                tool.BistSectorsRequest(
                    interval="1wk",
                    sectors=["XBANK"],
                )
            )
        ).payload
        self.assertEqual(out["n_bars"], 3)  # partial middle week kept on shared Jan 6
        self.assertEqual(out["n_fx_returns"], 3)  # four shared daily closes
        self.assertAlmostEqual(out["sectors"][0]["fx_beta"], 1)
        self.assertAlmostEqual(out["benchmark"]["usd_max_drawdown"], 9 / 11 - 1)

    async def test_flat_fx_has_undefined_beta(self):
        self.fx[:] = [10] * len(_DATES)
        out = (await tool.SPEC.handler(tool.BistSectorsRequest(sectors=["XBANK"]))).payload
        self.assertIsNone(out["sectors"][0]["fx_beta"])
        self.assertTrue(any("FX beta is unavailable" in note for note in out["notes"]))

    async def test_partial_failures_are_reported_and_required_series_fail(self):
        req = tool.BistSectorsRequest(sectors=["XBANK", "XSGRT"])
        out = (await tool.SPEC.handler(req)).payload
        self.assertEqual(out["failed"], {"XIST:XSGRT": "Index history unavailable."})
        self.assertEqual(len(out["sectors"]), 1)
        with self.assertRaisesRegex(ToolError, "Failed: XIST:XSGRT"):
            await tool.SPEC.handler(tool.BistSectorsRequest(sectors=["XSGRT"]))
        del self.paths[tool.FX]
        with self.assertRaisesRegex(
            ToolError, r"XIST:XSGRT: .*USDTRY=X: Index history unavailable"
        ):
            await tool.SPEC.handler(req)

    async def test_all_16_are_requested_and_intraday_is_rejected(self):
        await tool.SPEC.handler(tool.BistSectorsRequest())
        self.assertEqual(self.fetched.await_count, 18)
        self.assertIn("XIST:XTCRT", [c.args[0] for c in self.fetched.await_args_list])
        for params in ({"interval": "15m"}, {"sectors": ["XU100"]}, {"sectors": []}):
            with self.subTest(params=params), self.assertRaises(ValidationError):
                tool.BistSectorsRequest(**params)

    async def test_retail_index_history_does_not_route_through_stock_adjustments(self):
        frame = pd.DataFrame(
            [[1, 2, 1, 2, float("nan")]],
            columns=["Open", "High", "Low", "Close", "Volume"],
            index=pd.to_datetime(["2025-01-03"]),
        )
        day = dt.date(2025, 1, 3)
        with patch.object(bp, "indices", return_value=[]), patch.object(bp, "Index") as index:
            index.return_value.history.return_value = frame
            rows, is_index = await bist.fetch_daily("XTCRT", day, day)
        index.assert_called_once_with("XTCRT")
        index.return_value.history.assert_called_once_with(
            interval="1d",
            start=day.isoformat(),
            end=day.isoformat(),
        )
        self.assertTrue(is_index)
        self.assertIsNone(rows[0]["volume"])
