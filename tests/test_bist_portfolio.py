# SPDX-License-Identifier: Apache-2.0

import unittest
from unittest.mock import patch

from trading_r1.assembly import build_run_registry, builtin_skill_registry, catalog_tool_registry
from trading_r1.contract.spec import ToolError
from trading_r1.runtime.dispatch import dispatch
from trading_r1.runtime.skill_registry import SkillSessionState
from trading_r1.runtime.taxonomy import classify_issues, classify_skill
from trading_r1.runtime.tool_registry import ToolSessionState
from trading_r1.tools.compute.arithmetic import safe_eval
from trading_r1.tools.data.market import aligned_prices
from trading_r1.tools.data.pricing import Bars


class BistPortfolioTests(unittest.IsolatedAsyncioTestCase):
    async def test_builtin_skill_has_valid_taxonomy_and_loads_required_tools(self):
        skills = builtin_skill_registry()
        base = catalog_tool_registry()
        self.assertEqual(classify_issues(base, skills), [])
        skill = skills.get("bist-portfolio")
        self.assertIsNotNone(skill)
        self.assertEqual(classify_skill(skill), ("workflow", "portfolio"))
        required = {
            "data_market_bist_screen",
            "data_market_bist",
            "data_market_aligned_prices",
            "data_market_quotes",
            "compute_risk_correlation",
            "compute_risk_drawdown",
            "compute_portfolio_decomposition",
            "compute_portfolio_performance",
            "compute_portfolio_optimization",
        }
        self.assertEqual(set(skill.requires_tools), required)
        tool_state = ToolSessionState()
        run = build_run_registry(base, skills, tool_state, SkillSessionState(), lambda *_: None)
        loaded = await dispatch("load_skill", {"skills": [skill.name]}, registry=run)
        self.assertTrue(loaded.ok, loaded.text)
        self.assertEqual(set(loaded.payload["skills"][0]["tools_loaded"]), required)
        self.assertTrue(
            required <= {t["function"]["name"] for t in tool_state.visible_schemas(run)}
        )

    def test_calc_series_require_equal_lengths_nonzero_fx_and_positive_prices(self):
        for expression, message in (
            ("[2, 3] / [1]", "equal-length"),
            ("[2, 3] / [0, 1]", "division by zero"),
            ("returns([2, 0])", "positive prices"),
        ):
            with self.subTest(expression=expression), self.assertRaisesRegex(ToolError, message):
                safe_eval(expression)
        self.assertEqual(
            safe_eval("{'test': [6/3, 10+2], 'scalar': 2**3}"), {"test": [2, 12], "scalar": 8}
        )

    def test_calc_intermediate_series_are_validated_and_can_feed_each_other(self):
        values = {
            "local": [100, 132, 84],
            "fx": [10, 12, 12],
            "usd": "local / fx",
            "ret": "returns(usd)",
        }
        result = safe_eval("{'closes': usd, 'returns': ret}", values)
        self.assertEqual(result["closes"], [10, 11, 7])
        self.assertAlmostEqual(result["returns"][0], 0.1)
        self.assertAlmostEqual(result["returns"][1], -4 / 11)
        with self.assertRaisesRegex(ToolError, "not a finite number"):
            safe_eval("x", {"invalid": "[1e308] / [1e-308]", "x": 3})

    async def test_calc_keeps_long_series_in_evidence_and_only_renders_a_preview(self):
        tool_state = ToolSessionState()
        result = await dispatch(
            "calc",
            {
                "expression": "{'USD': stock / fx}",
                "values": {"stock": list(range(100, 400)), "fx": [10] * 300},
            },
            registry=catalog_tool_registry(),
            session_state=tool_state,
        )
        self.assertTrue(result.ok, result.text)
        series = tool_state.evidence.payloads[result.evidence_id]["result"]["USD"]
        self.assertEqual((len(series), series[0], series[-1]), (300, 10, 39.9))
        self.assertIn("300 values", result.text)
        self.assertLess(len(result.text), 400)

    async def test_usd_calculation_chains_aligned_prices_to_risk_and_benchmark(self):
        registry = catalog_tool_registry()
        session = ToolSessionState()

        async def call(name, args):
            output = await dispatch(name, args, registry=registry, session_state=session)
            self.assertTrue(output.ok, output.text)
            return f"prev:{output.evidence_id}", output.payload

        fx = [10, 11, 12, 11, 12, 13]
        paths = {
            "XIST:LOGO": [10, 9, 12, 11, 13, 10],
            "XIST:ENKAI": [5, 5.4, 4.5, 4.9, 5.3, 5.5],
            "XIST:XU100": [100, 95, 104, 89, 100, 102],
        }
        dates = [f"2025-01-{n:02}" for n in (2, 3, 6, 7, 8, 9)]

        async def bars(symbol, *_):
            values = (
                fx
                if symbol == "USDTRY=X"
                else [usd * rate for usd, rate in zip(paths[symbol], fx, strict=True)]
            )
            rows = [{"date": day, "close": value} for day, value in zip(dates, values, strict=True)]
            if symbol == "USDTRY=X":
                rows.insert(2, {"date": "2025-01-04", "close": 500})  # a non-shared date
            return Bars(rows, currency="TRY")

        with patch.object(aligned_prices, "fetch_bars", bars):
            aligned, data = await call(
                "data_market_aligned_prices",
                {
                    "symbols": ["XIST:LOGO", "XIST:ENKAI", "USDTRY=X", "XIST:XU100"],
                    "period": "3y",
                },
            )
        self.assertEqual(data["dates"], dates)
        self.assertEqual(data["per_symbol"]["USDTRY=X"]["dropped"], 1)
        usd, converted = await call(
            "calc",
            {
                "values": {
                    k: f"{aligned}.columns[{i}]" for i, k in enumerate(("l", "en", "fx", "idx"))
                }
                | {"usd_logo": "l / fx", "benchmark": "returns(idx / fx)"},
                "expression": (
                    '{"XIST:LOGO": usd_logo, "XIST:ENKAI": en / fx, "benchmark": benchmark}'
                ),
            },
        )
        self.assertEqual(converted["result"]["XIST:LOGO"], paths["XIST:LOGO"])
        prices = {symbol: f"{usd}.result.{symbol}" for symbol in ("XIST:LOGO", "XIST:ENKAI")}
        _, chained = await call(
            "calc",
            {
                "values": {
                    "stock": prices["XIST:LOGO"],
                    "first_fx_less_one": f"{aligned}.columns[2][0]-1",
                    "stock_returns": "returns(stock)",
                },
                "expression": "{'first_return': stock_returns[0], 'fx_check': first_fx_less_one}",
            },
        )
        self.assertAlmostEqual(chained["result"]["first_return"], -0.1)
        self.assertEqual(chained["result"]["fx_check"], 9)
        periods = f"{aligned}.periods_per_year"
        _, corr = await call("compute_risk_correlation", {"series": prices})
        self.assertEqual(corr["n_obs"], 5)
        self.assertLess(corr["matrix"]["XIST:LOGO"]["XIST:ENKAI"], 0)
        _, drawdown = await call(
            "compute_risk_drawdown", {"closes": prices["XIST:LOGO"], "dates": f"{aligned}.dates"}
        )
        self.assertAlmostEqual(drawdown["max_drawdown"], 10 / 13 - 1)
        optimized, _ = await call(
            "compute_portfolio_optimization",
            {"prices": prices, "periods_per_year": periods, "method": "risk_parity"},
        )
        decomposed, risk = await call(
            "compute_portfolio_decomposition",
            {"prices": prices, "periods_per_year": periods, "weights": f"{optimized}.weights"},
        )
        self.assertAlmostEqual(risk["holdings"][0]["risk_contribution"], 0.5, places=4)
        self.assertAlmostEqual(sum(h["risk_contribution"] for h in risk["holdings"]), 1)
        _, performance = await call(
            "compute_portfolio_performance",
            {
                "returns": f"{decomposed}.returns",
                "benchmark": f"{usd}.result.benchmark",
                "periods_per_year": periods,
            },
        )
        self.assertEqual(performance["n_obs"], 5)
        self.assertIsNotNone(performance["tracking_error"])
