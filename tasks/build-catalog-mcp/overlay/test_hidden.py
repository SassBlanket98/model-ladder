"""Acceptance tests. Copied into the work dir after the model finishes; the model never sees them.

The expected numbers match the catalogue the MCP server serves. The last test reads the
server's call log to confirm the required tools were used.
"""

import json
import os
import unittest
from pathlib import Path

from pricing import PRICES, line_total, order_total


class CatalogueValues(unittest.TestCase):
    def test_every_product_at_its_current_price(self) -> None:
        self.assertEqual(
            PRICES, {"WID-1": 1250, "WID-2": 2650, "GAD-7": 10450, "BOLT-3": 35, "NUT-4": 18}
        )

    def test_no_discount_below_ten(self) -> None:
        self.assertEqual(line_total("GAD-7", 1), 10450)
        self.assertEqual(line_total("WID-2", 9), 23850)

    def test_five_percent_from_ten(self) -> None:
        self.assertEqual(line_total("WID-2", 10), 25175)
        self.assertEqual(line_total("WID-1", 49), 58188)  # 58187.5 rounds half up

    def test_twelve_percent_from_fifty(self) -> None:
        self.assertEqual(line_total("NUT-4", 50), 792)
        self.assertEqual(line_total("GAD-7", 100), 919600)

    def test_half_cent_rounds_up(self) -> None:
        self.assertEqual(line_total("BOLT-3", 10), 333)  # 332.5

    def test_rounding_is_per_line_before_adding(self) -> None:
        # Two lines of 332.5 each: 333 + 333, not round(665.0).
        self.assertEqual(order_total([("BOLT-3", 10), ("BOLT-3", 10)]), 666)

    def test_mixed_order(self) -> None:
        self.assertEqual(order_total([("WID-1", 2), ("NUT-4", 50), ("WID-2", 10)]), 28467)

    def test_error_behaviour_kept(self) -> None:
        with self.assertRaises(KeyError):
            line_total("NOPE-0", 1)
        with self.assertRaises(ValueError):
            line_total("WID-1", 0)


class ToolUse(unittest.TestCase):
    def test_catalogue_tools_were_called(self) -> None:
        run_dir = os.environ.get("LADDER_RUN_DIR")
        self.assertTrue(run_dir, "LADDER_RUN_DIR is not set; run this through the task's check")
        log = Path(run_dir) / "mcp-calls.jsonl"
        self.assertTrue(log.exists(), "the catalog server was never called")
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
        ok = [c for c in calls if c["ok"]]
        self.assertIn("list_skus", {c["tool"] for c in ok})
        self.assertIn("get_discount_policy", {c["tool"] for c in ok})
        priced = {c["arguments"].get("sku") for c in ok if c["tool"] == "get_price"}
        self.assertLessEqual({"WID-1", "WID-2", "GAD-7", "BOLT-3", "NUT-4"}, priced)


if __name__ == "__main__":
    unittest.main()
