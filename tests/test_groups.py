import unittest

from bandctl.groups import Group, validate_groups


class GroupTests(unittest.TestCase):
    def test_valid_group_is_normalized_and_immutable(self):
        members = [12, 34]
        group = Group("  Games  ", 30, None, members)
        members.append(56)
        self.assertEqual((group.name, group.members), ("Games", (12, 34)))
        self.assertEqual(group.down_pct, 30)

    def test_rejects_invalid_names_and_percentages(self):
        for name in ("", "  ", "bad\nname", 3):
            with self.subTest(name=name), self.assertRaises(ValueError):
                Group(name, 10, None)
        for down, up in ((None, None), (0, None), (101, None), (True, None), (None, 1.5)):
            with self.subTest(down=down, up=up), self.assertRaises(ValueError):
                Group("games", down, up)

    def test_rejects_invalid_or_duplicate_members(self):
        for members in ((0,), (-1,), (True,), ("123",), (123, 123)):
            with self.subTest(members=members), self.assertRaises(ValueError):
                Group("games", 10, 20, members)

    def test_groups_cannot_overlap_or_have_case_insensitive_duplicate_names(self):
        first = Group("Games", 10, None, (12,))
        validate_groups([first, Group("Browser", None, 20, (34,))])
        with self.assertRaisesRegex(ValueError, "names"):
            validate_groups([first, Group("games", 5, None)])
        with self.assertRaisesRegex(ValueError, "multiple groups"):
            validate_groups([first, Group("Browser", 5, None, (12,))])
