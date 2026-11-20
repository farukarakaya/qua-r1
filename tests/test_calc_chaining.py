# SPDX-License-Identifier: Apache-2.0

import unittest

from trading_r1.assembly import catalog_tool_registry
from trading_r1.runtime.dispatch import dispatch
from trading_r1.runtime.tool_registry import ToolSessionState


class CalcChainingTests(unittest.IsolatedAsyncioTestCase):
    async def test_bist_quotes_in_both_inline_shapes_and_as_named_input(self):
        session = ToolSessionState()
        session.evidence.add(
            tool="quotes",
            args={},
            meta={},
            payload={"quotes": {"XIST:FROTO": {"price": 45}}, "sequence": [2, 4, 8]},
        )
        ref = "prev:e1.quotes.XIST:FROTO.price"
        fixtures = (
            ({"expression": ref}, 45),
            ({"values": {"p": f"[{ref}, {ref}*2]"}, "expression": "sum(p)"}, 135),
            ({"values": {"p": ref}, "expression": "p * 2"}, 90),
            ({"expression": '{"XIST:FROTO": sum(prev:e1.sequence[-2:])}'}, {"XIST:FROTO": 12}),
        )
        registry = catalog_tool_registry()
        for args, expected in fixtures:
            with self.subTest(args=args):
                result = await dispatch("calc", args, registry=registry, session_state=session)
                self.assertTrue(result.ok, result.text)
                self.assertEqual(result.payload["result"], expected)
                self.assertEqual(session.evidence.get(result.evidence_id).inputs, ("e1",))
