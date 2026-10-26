"""Percent parsing, unit conversion, and HTB splits. No root, no tc."""

from __future__ import annotations

import unittest

from bandctl.rates import (
    RateError,
    Unit,
    format_budget_input,
    format_bytes,
    format_rate,
    htb_plan,
    over_commit_message,
    parse_budget,
    parse_percent,
    percent_to_bps,
)


class PercentTests(unittest.TestCase):
    def test_plain_and_suffix(self):
        self.assertEqual(parse_percent("30"), 30)
        self.assertEqual(parse_percent("30%"), 30)
        self.assertEqual(parse_percent("  100 % "), 100)

    def test_empty(self):
        self.assertIsNone(parse_percent("  ", allow_empty=True))
        with self.assertRaises(RateError):
            parse_percent("  ")

    def test_rejects_zero_over_and_junk(self):
        for text in ("0", "101", "5.5", "10M", "-3", "abc"):
            with self.assertRaises(RateError):
                parse_percent(text)


class BudgetTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_budget("100", Unit.MBIT), 100_000_000)
        self.assertEqual(parse_budget("1", Unit.MB), 8_000_000)
        self.assertIsNone(parse_budget("  ", Unit.MBIT))

    def test_rejects_non_positive(self):
        with self.assertRaises(RateError):
            parse_budget("0", Unit.MBIT)
        with self.assertRaises(RateError):
            parse_budget("fast", Unit.MB)

    def test_rejects_non_finite_and_sub_bit_budgets(self):
        for unit in Unit:
            for text in ("nan", "inf", "-inf", "1e308", "1e-100"):
                with self.subTest(unit=unit, text=text):
                    with self.assertRaises(RateError):
                        parse_budget(text, unit)
        self.assertEqual(parse_budget("0.000001", Unit.MBIT), 1)


class ConversionTests(unittest.TestCase):
    def test_format_byte_counts(self):
        for count, expected in ((-1, "0 B"), (999, "999 B"), (1000, "1.00 kB"),
                                (1_500_000, "1.50 MB"), (12_500_000_000, "12.5 GB")):
            with self.subTest(count=count):
                self.assertEqual(format_bytes(count), expected)
    def test_thirty_percent_of_100_mbit(self):
        capacity = 100_000_000
        bps = percent_to_bps(30, capacity)
        self.assertEqual(bps, 30_000_000)
        self.assertEqual(format_rate(bps, Unit.MBIT), "30.0 Mbit/s")
        self.assertEqual(format_rate(bps, Unit.MB), "3.75 MB/s")

    def test_budget_round_trip_text(self):
        self.assertEqual(format_budget_input(100_000_000, Unit.MBIT), "100")
        self.assertEqual(format_budget_input(100_000_000, Unit.MB), "12.5")


class PlanTests(unittest.TestCase):
    def test_single_share_keeps_its_ceil(self):
        rates, ceils, default = htb_plan([30_000_000], 100_000_000)
        self.assertEqual(ceils, [30000])
        self.assertEqual(rates, [30000])
        self.assertEqual(default, 70000)

    def test_over_commit_scales_rates_and_keeps_ceils(self):
        # Three 50% ceilings. Rates shrink. Ceils stay at half the parent.
        rates, ceils, default = htb_plan([50_000_000] * 3, 100_000_000)
        self.assertEqual(ceils, [50000, 50000, 50000])
        self.assertEqual(sum(rates), 99999)
        self.assertTrue(all(rate < ceil for rate, ceil in zip(rates, ceils)))
        self.assertEqual(default, 1)
        self.assertEqual(rates, [33333, 33333, 33333])

    def test_over_commit_message(self):
        self.assertIsNone(over_commit_message("download", [30, 20]))
        text = over_commit_message("download", [60, 50])
        self.assertIn("110%", text or "")


if __name__ == "__main__":
    unittest.main()
