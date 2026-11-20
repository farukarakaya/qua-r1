# SPDX-License-Identifier: Apache-2.0

import json
import unittest
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from borsapy.cache import Cache
from pydantic import ValidationError

from trading_r1.assembly import catalog_tool_registry
from trading_r1.contract.spec import ToolError
from trading_r1.tools.data.market.bist_screen import SPEC, BistScreenRequest

# Raw getScreenerDataNEW columns. The similarly named IDs from the criteria picker
# are intentionally present as distractors; neither criteria_8 (TRY cap) nor 60 is USD cap.
_STOCKS = [
    {
        "symbol": "GARAN",
        "name": "Garanti",
        "sector": "Bankacılık",
        "criteria_8": 600000,
        "criteria_9": 12000,
        "criteria_60": 50,
        "criteria_26": 80,
        "criteria_28": 5,
        "criteria_30": 1.2,
        "criteria_33": 3,
        "criteria_422": 47,
    },
    {
        "symbol": "logo",
        "name": "Logo Yazılım",
        "sector": "Teknoloji",
        "criteria_8": 11020,
        "criteria_9": 218.35,
        "criteria_60": 30,
        "criteria_26": 1.44,
        "criteria_28": 11.79,
        "criteria_29": 3.9,
        "criteria_30": 2.25,
        "criteria_33": 3.31,
        "criteria_61": 96.17,
        "criteria_73": -57.79,
        "criteria_119": 9.59,
        "criteria_422": 22.2,
    },
    {
        "symbol": "SMALL",
        "name": "Small stock",
        "criteria_8": 3000,
        "criteria_9": 59,
        "criteria_60": 200,
        "criteria_26": 0.3,
        "criteria_28": 6,
        "criteria_422": 23,
    },
    {"symbol": "NODATA", "name": "No P/E data", "criteria_9": 155, "criteria_422": 22},
]


class BistScreenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = Mock()
        self.provider.PAGE_URL = "fixture"
        self.provider._post.side_effect = self._post
        self.cache = Cache()
        self.provider._cache_get.side_effect = self.cache.get
        self.provider._cache_set.side_effect = self.cache.set
        self.enterContext(
            patch(
                "borsapy._providers.isyatirim_screener.get_screener_provider",
                return_value=self.provider,
            )
        )
        self.calls = []
        self.before_fetch = lambda _: None
        self.responses = {}

    def _post(self, url, *, json, headers):
        (criterion,) = json["criterias"]
        id_ = criterion[0]
        self.calls.append((id_, json))
        self.before_fetch(id_)
        if id_ in self.responses:
            body = self.responses[id_]
            if isinstance(body, Exception):
                raise body
        else:
            body = self._body(id_)
        return SimpleNamespace(json=lambda: body)

    @staticmethod
    def _body(id_):
        return {
            "d": json.dumps(
                [
                    {"Hisse": f"{row['symbol']} - {row['name']}", id_: row[f"criteria_{id_}"]}
                    for row in _STOCKS
                    if f"criteria_{id_}" in row
                ]
            )
        }

    async def test_full_universe_raw_ids_and_units(self):
        self.assertIs(catalog_tool_registry().get("data_market_bist_screen"), SPEC)
        result = await SPEC.handler(
            BistScreenRequest(market_cap_usd_min=100, roe_min=20, pe_max=15)
        )
        rows = result.payload["rows"]
        self.assertEqual([r["symbol"] for r in rows], ["XIST:GARAN", "XIST:LOGO"])
        self.assertEqual(result.payload["total_matched"], 2)
        self.assertFalse(result.payload["truncated"])
        self.assertEqual(
            rows[1],
            {
                "symbol": "XIST:LOGO",
                "name": "Logo Yazılım",
                "sector": None,
                "market_cap_usd": 218.35,
                "avg_volume_3m_usd": 1.44,
                "pe": 11.79,
                "pb": 2.25,
                "ev_ebitda": 3.9,
                "roe": 22.2,
                "net_margin": 9.59,
                "net_income_growth": -57.79,
                "dividend_yield": 3.31,
                "upside_potential": 96.17,
            },
        )
        self.assertIn("USD millions", result.text)
        self.assertIn("22.20%", result.text)
        self.assertIn("11.79x", result.text)
        self.assertIn("BRSA", result.text)
        for id_, payload in self.calls:
            self.assertNotIn(id_, {"8", "60"})
            self.assertEqual(payload["criterias"][0][-1], "False")
            self.assertEqual(payload["endeks"], "")

    async def test_sort_before_limit_and_retain_uncovered_stocks(self):
        result = await SPEC.handler(BistScreenRequest(sort_by="pe", limit=2))
        self.assertEqual(result.payload["total_matched"], 4)
        self.assertTrue(result.payload["truncated"])
        self.assertEqual(
            [r["symbol"] for r in result.payload["rows"]], ["XIST:GARAN", "XIST:SMALL"]
        )
        full = await SPEC.handler(BistScreenRequest(sort_by="roe"))
        uncovered = next(r for r in full.payload["rows"] if r["symbol"] == "XIST:NODATA")
        self.assertIsNone(uncovered["pe"])
        self.assertIsNone(uncovered["upside_potential"])
        self.assertEqual([r["roe"] for r in full.payload["rows"]], [47, 23, 22.2, 22])

    async def test_valuation_multiples_rank_nonpositive_last_and_filter_ev_ebitda(self):
        stocks = [
            {
                "symbol": name,
                "name": name,
                "criteria_9": 100,
                "criteria_28": value,
                "criteria_29": value,
                "criteria_30": value,
            }
            for name, value in (("NEG", -12), ("ZERO", 0), ("CHEAP", 4), ("EXP", 12), ("NA", None))
        ]
        with patch(f"{__name__}._STOCKS", stocks):
            for metric in ("pe", "pb", "ev_ebitda"):
                with self.subTest(metric=metric):
                    result = await SPEC.handler(BistScreenRequest(sort_by=metric))
                    self.assertEqual(
                        [r["symbol"] for r in result.payload["rows"]],
                        ["XIST:CHEAP", "XIST:EXP", "XIST:NEG", "XIST:ZERO", "XIST:NA"],
                    )
            result = await SPEC.handler(BistScreenRequest(ev_ebitda_min=0.1, ev_ebitda_max=8))
        self.assertEqual([r["symbol"] for r in result.payload["rows"]], ["XIST:CHEAP"])

    async def test_criteria_fetches_overlap_after_market_cap(self):
        ready = Barrier(2, timeout=3)

        def meet(criterion):
            if criterion in ("26", "28"):
                ready.wait()

        self.before_fetch = meet
        result = await SPEC.handler(BistScreenRequest())
        self.assertEqual(result.payload["total_matched"], len(_STOCKS))
        self.assertEqual(self.calls[0][0], "9")
        self.assertCountEqual(
            [id_ for id_, _ in self.calls],
            ["9", "26", "28", "29", "30", "33", "61", "73", "119", "422"],
        )

    async def test_index_sector_and_recommendation_filters(self):
        html = (
            b'<select id="current_prefix_ddlStockSector">'
            b'<option value="0043">Teknoloji</option></select>'
        )
        self.provider._get.return_value = SimpleNamespace(content=html)
        with patch(
            "borsapy.Index", return_value=SimpleNamespace(component_symbols=["LOGO"])
        ) as index:
            result = await SPEC.handler(
                BistScreenRequest(index="BIST100", sector="teknoloji", recommendation="AL")
            )
        index.assert_called_once_with("XU100")
        self.assertEqual([r["symbol"] for r in result.payload["rows"]], ["XIST:LOGO"])
        self.assertEqual(result.payload["rows"][0]["sector"], "Teknoloji")
        for _, payload in self.calls:
            self.assertEqual(payload["sektor"], "0043")
            self.assertEqual(payload["oneri"], "AL")

    async def test_empty_screen_is_not_an_error(self):
        no_matches = await SPEC.handler(BistScreenRequest(market_cap_usd_max=1))
        self.assertEqual(no_matches.payload["rows"], [])
        self.assertEqual(no_matches.payload["total_matched"], 0)
        self.assertFalse(no_matches.payload["truncated"])
        self.responses["9"] = {"d": "[]"}
        empty = await SPEC.handler(BistScreenRequest(recommendation="AL"))
        self.assertEqual(empty.payload["rows"], [])
        self.assertEqual(empty.payload["unavailable_metrics"], [])

    async def test_malformed_universe_is_not_cached_or_treated_as_empty(self):
        for body in (
            {"error": "unavailable"},
            {"d": "{}"},
            {"d": "invalid json"},
            {"d": '[{"Hisse":"LOGO - Logo","8":11020,"60":218.35}]'},
            {"d": '[{"Hisse":"LOGO","9":"bad"}]'},
        ):
            self.responses["9"] = body
            with (
                self.subTest(body=body),
                self.assertRaisesRegex(ToolError, "invalid data for market_cap_usd"),
            ):
                await SPEC.handler(BistScreenRequest())
        self.responses["9"] = {"d": "[]"}
        with self.assertRaisesRegex(ToolError, "empty BIST universe"):
            await SPEC.handler(BistScreenRequest())
        self.responses["9"] = httpx.ReadTimeout("provider down")
        with self.assertRaisesRegex(ToolError, "could not fetch market_cap_usd"):
            await SPEC.handler(BistScreenRequest())
        del self.responses["9"]
        self.assertEqual((await SPEC.handler(BistScreenRequest())).payload["total_matched"], 4)

    async def test_filter_and_sort_fail_explicitly_but_display_only_failure_is_reported(self):
        self.responses["422"] = {"error": "unavailable"}
        self.responses["61"] = httpx.ReadTimeout("provider down")
        for req, metric in (
            (BistScreenRequest(roe_min=20), "roe"),
            (BistScreenRequest(sort_by="upside_potential"), "upside_potential"),
        ):
            with (
                self.subTest(metric=metric),
                self.assertRaisesRegex(ToolError, f"for {metric}|fetch {metric}"),
            ):
                await SPEC.handler(req)
        result = await SPEC.handler(BistScreenRequest(market_cap_usd_min=100))
        self.assertEqual(result.payload["total_matched"], 3)
        self.assertCountEqual(result.payload["unavailable_metrics"], ["roe", "upside_potential"])
        self.assertIn("unavailable_metrics", result.text)
        self.assertEqual(result.payload["rows"][0]["roe"], None)
        self.assertNotIn("net_income_growth", result.payload["unavailable_metrics"])

    async def test_missing_index_constituents_do_not_fall_back(self):
        with (
            patch("borsapy.Index", return_value=SimpleNamespace(component_symbols=[])),
            self.assertRaisesRegex(ToolError, "No BIST index constituents"),
        ):
            await SPEC.handler(BistScreenRequest(index="BIST30"))

    def test_invalid_bounds(self):
        with self.assertRaisesRegex(ValidationError, "market_cap_usd_min cannot exceed"):
            BistScreenRequest(market_cap_usd_min=100, market_cap_usd_max=1)
        with self.assertRaisesRegex(ValidationError, "ev_ebitda_min cannot exceed"):
            BistScreenRequest(ev_ebitda_min=8, ev_ebitda_max=4)
        with self.assertRaises(ValidationError):
            BistScreenRequest(avg_volume_3m_usd_min=float("inf"))
