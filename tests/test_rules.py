"""Automatic rule matching and validation, with no kernel access."""

import unittest

from bandctl.rules import AutoRule, first_match


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

    def test_invalid_rules_are_rejected(self):
        for name, down, up in ((" ", 10, None), ("x\n", 10, None), ("x", None, None),
                               ("x", 0, None), ("x", None, 101), ("x", True, None)):
            with self.subTest(name=name, down=down, up=up):
                with self.assertRaises(ValueError):
                    AutoRule(name, down, up)


if __name__ == "__main__":
    unittest.main()
