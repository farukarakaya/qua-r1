# SPDX-License-Identifier: Apache-2.0

import datetime as dt
import math
import os
import unittest
from unittest.mock import patch

import borsapy as bp
import pandas as pd
from pydantic import ValidationError

from trading_r1.contract.spec import ToolError
from trading_r1.host.daemon import copied_environment
from trading_r1.tools.data import bist
from trading_r1.tools.data.macro import turkey

_TODAY = dt.date(2026, 11, 20)
_CODES = ["TP.APIFON4", "TP.RK.T1.Y", "TP.AB.C2", "TP.TRY.MT02", "TP.KTF10"]


class TurkeyMacroTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cpi = pd.DataFrame(
            {
                "YearlyInflation": [29.77, 29.73, 31.51, 31.75, 35.0],
                "MonthlyInflation": [2.58, 1.84, 1.84, 1.78, 1.5],
            },
            index=pd.to_datetime(
                ["2026-10-01", "2026-09-01", "2026-08-01", "2026-07-01", "2026-06-01"]
            ),
        )
        bonds = pd.DataFrame(
            [
                ("US900123AA01", "2025-10-21", 9),  # matured
                ("US900123AA02", "2028-11-20", 5),
                ("US900123AA03", "2029-02-21", 5.5),  # farther from 2y target
                ("US900123AA04", "2031-11-20", 0),
                ("US900123AA05", "2031-10-21", 6),
                ("US900123AA06", "2036-11-20", math.inf),
                ("US900123AA07", "2037-02-21", 7),
                ("US900123AA08", None, 10),
            ],
            columns=["isin", "maturity", "ask_yield"],
        )
        evds = pd.DataFrame(
            {
                "TP_APIFON4": [36, 50, 37, 37],
                "TP.RK.T1.Y": [math.nan, math.nan, 107.76, 103.2],
                "TP.AB.C2": [58136, 40000, 56000, 57000],
                "TP.TRY.MT02": [42.22, 46, 43, 45],
                "TP.KTF10": [59.61, 65, 60, 61],
                "TP.CUSTOM-1": ["100", "50", None, "80"],
            },
            index=pd.to_datetime(["2026-11-01", "2025-11-01", "2026-10-01", "2026-08-01"]),
        )
        self.overlay = self.enterContext(patch.object(bist.credentials, "overlay", return_value={}))
        clock = self.enterContext(patch.object(turkey.dt, "datetime"))
        clock.now.return_value.date.return_value = _TODAY
        inflation = self.enterContext(patch.object(bp, "Inflation"))
        self.cpi = inflation.return_value.tufe
        self.cpi.return_value = cpi
        self.bonds = self.enterContext(patch.object(bp, "eurobonds", return_value=bonds))
        self.evds = self.enterContext(patch.object(bp, "evds_download", return_value=evds))
        self.set_key = self.enterContext(patch.object(bp, "set_evds_key"))
        self.clear_key = self.enterContext(patch.object(bp, "clear_evds_key"))

    async def test_missing_key_returns_cpi_and_bonds_but_rejects_custom_request(self):
        result = await turkey.SPEC.handler(turkey.TurkeyRequest())
        out = result.payload
        self.assertEqual(
            out["inflation"]["latest"], {"date": "2026-10-01", "yoy": 29.77, "mom": 2.58}
        )
        self.assertAlmostEqual(out["inflation"]["yoy_change_3m"], -1.98)
        self.assertAlmostEqual(out["inflation"]["mom_change_3m"], 0.8)
        self.assertEqual(len(out["inflation"]["observations"]), 4)
        self.assertEqual(
            {bucket: (row["isin"], row["ask_yield"]) for bucket, row in out["eurobonds"].items()},
            {"1-3y": ("US900123AA02", 5), "3-7y": ("US900123AA05", 6), "7y+": ("US900123AA07", 7)},
        )
        self.assertEqual(out["evds"], {})
        self.assertIsNone(out["real_funding_rate"])
        self.assertIn("EVDS_API_KEY is unset", result.text)
        with self.assertRaisesRegex(ToolError, "EVDS_API_KEY is required"):
            await turkey.SPEC.handler(turkey.TurkeyRequest(evds_codes=["TP.EXTRA"]))
        self.evds.assert_not_called()
        self.cpi.assert_called_once_with(limit=12)
        self.bonds.assert_called_once_with(currency="USD")

    async def test_batched_evds_series_and_real_rates(self):
        self.overlay.return_value = {"EVDS_API_KEY": "  example-key  "}
        with patch.dict(os.environ, clear=True):
            out = (
                await turkey.SPEC.handler(
                    turkey.TurkeyRequest(
                        period="6mo",
                        view="series",
                        evds_codes=["TP_CUSTOM-1", "TP_APIFON4"],
                    )
                )
            ).payload
            self.assertNotIn("EVDS_API_KEY", os.environ)
        self.set_key.assert_called_once_with("example-key")
        self.clear_key.assert_called_once_with()
        self.evds.assert_called_once_with(
            [*_CODES, "TP.CUSTOM-1"],
            start=dt.date(2025, 8, 1),
            end=_TODAY,
            frequency="monthly",
            aggregation="last",
        )
        self.cpi.assert_called_once_with(limit=6)
        self.assertEqual(len(out["inflation"]["observations"]), 5)
        self.assertAlmostEqual(out["real_funding_rate"], 6.23)
        self.assertAlmostEqual(out["real_deposit_rate"], 12.45)
        self.assertEqual(out["evds"]["funding_rate"]["change_3m"], -1)
        self.assertEqual(out["evds"]["funding_rate"]["change_12m"], -14)
        self.assertEqual(out["evds"]["reer_cpi"]["date"], "2026-10-01")
        self.assertEqual(out["evds"]["reer_cpi"]["unit"], "index (2025=100)")
        self.assertIsNone(out["evds"]["reer_cpi"]["change_3m"])  # July is missing
        self.assertEqual(
            out["evds"]["TP.CUSTOM-1"]["observations"],
            [
                {"date": "2026-08-01", "value": 80},
                {"date": "2026-11-01", "value": 100},
            ],
        )

    async def test_configured_evds_failure_is_explicit_and_sanitized(self):
        self.overlay.return_value = {"EVDS_API_KEY": "example-secret"}
        self.evds.side_effect = RuntimeError("upstream echoed example-secret")
        with self.assertRaisesRegex(ToolError, "CBRT EVDS batch failed") as ctx:
            await turkey.SPEC.handler(turkey.TurkeyRequest())
        self.assertNotIn("example-secret", str(ctx.exception))
        self.assertTrue(ctx.exception.__suppress_context__)
        self.clear_key.assert_called_once_with()

    async def test_no_silent_success_for_missing_custom_series_or_bad_cpi(self):
        self.overlay.return_value = {"EVDS_API_KEY": "example-key"}
        with self.assertRaisesRegex(ToolError, "no usable observations.*TP.UNKNOWN"):
            await turkey.SPEC.handler(turkey.TurkeyRequest(evds_codes=["TP.UNKNOWN"]))
        self.cpi.return_value = pd.DataFrame()
        with self.assertRaisesRegex(ToolError, "invalid CPI history"):
            await turkey.SPEC.handler(turkey.TurkeyRequest())
        self.evds.return_value = None
        with self.assertRaisesRegex(ToolError, "CBRT EVDS batch failed"):
            await turkey.SPEC.handler(turkey.TurkeyRequest())

    def test_custom_code_validation(self):
        for codes in (["tp.example"], ["TP/X"], [""], ["TCMB\n"], ["TP.X"] * 9):
            with self.subTest(codes=codes), self.assertRaises(ValidationError):
                turkey.TurkeyRequest(evds_codes=codes)

    def test_daemon_copies_configured_evds_key(self):
        self.assertEqual(
            copied_environment({"EVDS_API_KEY": "example-key", "UNRELATED_API_KEY": "other"}),
            {"EVDS_API_KEY": "example-key"},
        )
