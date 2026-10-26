"""Automatic rule matching and validation, with no kernel access."""

import unittest
from datetime import time

from bandctl.procscan import ProcessInfo
from bandctl.rules import AutoRule, RuleTracker, first_match
from bandctl.schedule import DailyWindow


class RuleTests(unittest.TestCase):
    def test_name_match_uses_case_insensitive_substring(self):
        rule = AutoRule(" Steam ", 30, None)
        self.assertEqual(rule.name, "Steam")
        self.assertTrue(rule.matches("steamwebhelper"))
        self.assertFalse(rule.matches("browser"))

    def test_first_matching_rule_wins(self):
        first = AutoRule("steam", 30, None)
        second = AutoRule("web", None, 10)
        self.assertIs(first_match([first, second], "steamwebhelper"), first)
        self.assertIsNone(first_match([first, second], "curl"))

    def test_only_active_scheduled_rules_match(self):
        scheduled = AutoRule("steam", 30, None, DailyWindow(time(22), time(6)))
        fallback = AutoRule("steam", None, 10)
        self.assertIs(first_match([scheduled, fallback], "steam", time(23)), scheduled)
        self.assertIs(first_match([scheduled, fallback], "steam", time(6)), fallback)
        self.assertIs(first_match([scheduled, fallback], "steam"), fallback)
        tracker = RuleTracker()
        proc = ProcessInfo(42, "steam", "steam", "user", 1, 100)
        self.assertEqual(tracker.candidates([proc], [scheduled], set(), 1, time(12)), [])
        self.assertEqual(tracker.candidates([proc], [scheduled], set(), 2, time(23)), [(proc, scheduled)])

    def test_invalid_rules_are_rejected(self):
        for name, down, up in ((" ", 10, None), ("x\n", 10, None), ("x", None, None),
                               ("x", 0, None), ("x", None, 101), ("x", True, None)):
            with self.subTest(name=name, down=down, up=up):
                with self.assertRaises(ValueError):
                    AutoRule(name, down, up)

    def test_tracker_uses_process_identity_and_respects_existing_limits(self):
        tracker = RuleTracker()
        rule = AutoRule("steam", 30, None)
        first = ProcessInfo(42, "steam", "steam", "user", 1, 100)
        unknown = ProcessInfo(43, "steam", "steam", "user", 1)
        self.assertEqual(tracker.candidates([first, unknown], [rule], set(), 1), [(first, rule)])
        tracker.applied(first)
        self.assertEqual(tracker.candidates([first], [rule], set(), 2), [])
        replacement = ProcessInfo(42, "steam", "steam", "user", 1, 200)
        self.assertEqual(tracker.candidates([replacement], [rule], set(), 3), [(replacement, rule)])
        self.assertEqual(tracker.candidates([replacement], [rule], {42}, 4), [])
        self.assertEqual(tracker.candidates([replacement], [rule], set(), 5), [])

    def test_failed_attempt_waits_before_retry_and_disappeared_state_expires(self):
        tracker = RuleTracker()
        rule = AutoRule("steam", None, 10)
        proc = ProcessInfo(42, "steam", "steam", "user", 1, 100)
        self.assertEqual(tracker.candidates([proc], [rule], set(), 10), [(proc, rule)])
        tracker.failed(proc, 10)
        self.assertEqual(tracker.candidates([proc], [rule], set(), 24), [])
        self.assertEqual(tracker.candidates([proc], [rule], set(), 25), [(proc, rule)])
        tracker.applied(proc)
        tracker.candidates([], [rule], set(), 400)
        self.assertEqual(tracker.candidates([proc], [rule], set(), 401), [(proc, rule)])


if __name__ == "__main__":
    unittest.main()
