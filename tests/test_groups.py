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
        for name in ("", "  ", "bad\nname", "\ud800", 3):
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


class ActiveGroupsTests(unittest.TestCase):
    def test_groups_are_sorted_with_sorted_members_and_skip_individual_shares(self):
        from bandctl.groups import active_groups
        from bandctl.shaper import Share

        shares = {
            43: Share(43, 30, None, 10, "/", group="Work"),
            42: Share(42, 30, None, 10, "/", group="Work"),
            9: Share(9, None, 5, 11, "/", group="Audio"),
            1: Share(1, 10, None, 12, "/"),
        }
        self.assertEqual(active_groups(shares), [Group("Audio", None, 5, (9,)), Group("Work", 30, None, (42, 43))])

    def test_conflicting_group_state_is_rejected(self):
        from bandctl.groups import active_groups
        from bandctl.shaper import Share

        for second in (Share(43, 20, None, 10, "/", group="work"),
                       Share(43, 30, None, 11, "/", group="work"),
                       Share(43, 30, None, 10, "/", group="WORK")):
            with self.subTest(second=second), self.assertRaisesRegex(ValueError, "conflicting"):
                active_groups({42: Share(42, 30, None, 10, "/", group="work"), 43: second})
