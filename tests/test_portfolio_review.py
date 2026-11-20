# SPDX-License-Identifier: Apache-2.0

import unittest

from trading_r1.assembly import catalog_tool_registry
from trading_r1.runtime.dispatch import dispatch
from trading_r1.runtime.tool_registry import ToolSessionState


class PortfolioReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_book_funds_stage_without_forcing_an_unauthorized_sale(self):
        outcome = await dispatch(
            "compute_portfolio_review",
            {
                "holdings": {"HOLDCO": 400, "OPERATING": 500},
                "opening_cash": 100,
                "commission_rate": 0.01,
                "spread_rate": 0.01,
                "stage_trades": {"REPLACEMENT": 95},
                "full_trades": {"HOLDCO": -400, "OPERATING": -200, "REPLACEMENT": 650},
                "sale_basis": {
                    "HOLDCO": {"reason": "review_only", "original_thesis": "NAV unknown"},
                    "OPERATING": {
                        "reason": "payout_cut_only",
                        "original_thesis": "The core business generates cash",
                        "contrary_filing": "[1] Reduced cash distribution",
                    },
                },
            },
            registry=catalog_tool_registry(),
            session_state=ToolSessionState(),
        )
        self.assertTrue(outcome.ok, outcome.text)
        hold, stage, target = (outcome.payload[k] for k in ("hold", "first_stage", "full_target"))
        self.assertEqual(hold["closing_cash"], 100)
        self.assertEqual(stage["status"], "funded")
        self.assertEqual(stage["sells"], 0)
        self.assertEqual(stage["external_cash"], 0)
        self.assertAlmostEqual(stage["closing_cash"], 3.10)  # 100 - 95 - 0.95 - 0.95
        self.assertAlmostEqual(stage["turnover_one_way"], 0.0475)
        self.assertEqual(target["status"], "conditional")
        self.assertEqual(target["blocked_sales"], ["HOLDCO", "OPERATING"])
        self.assertAlmostEqual(target["closing_cash"], 25)  # 100 + 600 - 650 - 12.5 - 12.5

    async def test_mandate_permission_only_unlocks_explicit_mandate_changes(self):
        bases = {
            "review": {"reason": "review_only"},
            "cut": {"reason": "payout_cut_only"},
            "unsupported": {"reason": "thesis_break", "original_thesis": "Licensed plant"},
            "change": {"reason": "mandate_change"},
        }
        holdings = dict.fromkeys([*bases, "missing"], 100)
        for permission in (True, False):
            with self.subTest(permission=permission):
                result = await dispatch(
                    "compute_portfolio_review",
                    dict(
                        holdings=holdings,
                        opening_cash=0,
                        commission_rate=0,
                        spread_rate=0,
                        stage_trades={},
                        full_trades=dict.fromkeys(holdings, -100),
                        mandate_sales_authorized=permission,
                        sale_basis=bases,
                    ),
                    registry=catalog_tool_registry(),
                )
                self.assertTrue(result.ok, result.text)
                blocked = set(result.payload["full_target"]["blocked_sales"])
                self.assertEqual(blocked, set(holdings) - ({"change"} if permission else set()))

    async def test_confirmed_cash_and_cited_thesis_break_are_distinct_from_income(self):
        outcome = await dispatch(
            "compute_portfolio_review",
            {
                "holdings": {"OPERATING": 500},
                "opening_cash": 0,
                "external_cash": 20,
                "commission_rate": 0.01,
                "spread_rate": 0.01,
                "stage_trades": {"OPERATING": -100, "REPLACEMENT": 110},
                "full_trades": {"REPLACEMENT": 200},
                "sale_basis": {
                    "OPERATING": {
                        "reason": "thesis_break",
                        "original_thesis": "Its only plant was fully licensed",
                        "contrary_filing": "[1] License revoked",
                    }
                },
            },
            registry=catalog_tool_registry(),
            session_state=ToolSessionState(),
        )
        self.assertTrue(outcome.ok, outcome.text)
        self.assertEqual(outcome.payload["first_stage"]["status"], "funded")
        self.assertAlmostEqual(outcome.payload["first_stage"]["closing_cash"], 5.80)
        self.assertEqual(outcome.payload["full_target"]["status"], "unfunded")

    async def test_sale_basis_rejects_unknown_fields(self):
        outcome = await dispatch(
            "compute_portfolio_review",
            {
                "holdings": {"OPERATING": 500},
                "opening_cash": 0,
                "commission_rate": 0,
                "spread_rate": 0,
                "stage_trades": {},
                "full_trades": {"OPERATING": -100},
                "sale_basis": {"OPERATING": {"reason": "thesis_break", "thesis": "Typo"}},
            },
            registry=catalog_tool_registry(),
        )
        self.assertFalse(outcome.ok)
