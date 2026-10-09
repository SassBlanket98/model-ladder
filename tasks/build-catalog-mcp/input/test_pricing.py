import unittest

from pricing import line_total, order_total


class PricingBasics(unittest.TestCase):
    def test_single_small_widget(self) -> None:
        self.assertEqual(line_total("WID-1", 1), 1250)

    def test_order_adds_lines(self) -> None:
        self.assertEqual(order_total([("WID-1", 2), ("BOLT-3", 4)]), 2640)

    def test_unknown_sku(self) -> None:
        with self.assertRaises(KeyError):
            line_total("NOPE-0", 1)

    def test_bad_quantity(self) -> None:
        for bad in (0, -1, 1.5, True):
            with self.assertRaises(ValueError):
                line_total("WID-1", bad)


if __name__ == "__main__":
    unittest.main()
