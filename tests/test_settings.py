"""Settings parsing does not touch the host or apply limits."""
import json
import unittest
from datetime import time

from bandctl.groups import Group
from bandctl.rules import AutoRule
from bandctl.schedule import DailyWindow
from bandctl.settings import GroupSettings, Settings, dumps, loads


class SettingsTests(unittest.TestCase):
    def settings(self):
        return Settings("eth0", 100_000_000, None,
                        (AutoRule("steam", 20, 5, DailyWindow(time(18), time(23))),),
                        (GroupSettings(Group("work", 30, None, (42, 43)), (100, 101)),),
                        "12345678-1234-4234-8234-123456789abc")

    def test_round_trip_includes_rules_schedules_groups_and_budgets(self):
        settings = self.settings()
        self.assertEqual(loads(dumps(settings)), settings)
        self.assertNotIn("cmdline", dumps(settings))

    def test_rejects_duplicate_unknown_and_missing_fields(self):
        text = dumps(self.settings())
        for changed in (text.replace('"version": 1,', '"version": 1, "version": 1,'),
                        text.replace('"version": 1,', '"extra": 1, "version": 1,'),
                        text.replace('"version": 1,', '')):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                loads(changed)

    def test_rejects_bad_rule_or_schedule(self):
        data = json.loads(dumps(self.settings()))
        for field, value in (("name", None), ("down", True), ("start", "25:00"), ("end", None)):
            changed = json.loads(json.dumps(data))
            changed["rules"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                loads(json.dumps(changed))

    def test_rejects_bad_budgets_interface_and_pid_identity(self):
        data = json.loads(dumps(self.settings()))
        for field, value in (("interface", "../bad"), ("down_budget_bps", 0), ("up_budget_bps", True)):
            changed = json.loads(json.dumps(data))
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                loads(json.dumps(changed))
        data["groups"][0]["members"][0]["start_ticks"] = None
        with self.assertRaises(ValueError):
            loads(json.dumps(data))

    def test_groups_require_a_valid_boot_identity(self):
        data = json.loads(dumps(self.settings()))
        for boot_id in (None, "invalid", True, "12345678-1234-4234-8234-123456789ABC"):
            data["boot_id"] = boot_id
            with self.subTest(boot_id=boot_id), self.assertRaises(ValueError):
                loads(json.dumps(data))
        data["groups"] = []
        data["boot_id"] = None
        self.assertIsNone(loads(json.dumps(data)).boot_id)

    def test_rejects_overlapping_groups(self):
        data = json.loads(dumps(self.settings()))
        extra = json.loads(json.dumps(data["groups"][0]))
        extra["name"] = "other"
        data["groups"].append(extra)
        with self.assertRaisesRegex(ValueError, "multiple groups"):
            loads(json.dumps(data))


class SettingsFileTests(unittest.TestCase):
    def test_import_rejects_fifo_without_waiting_for_a_writer(self):
        import os
        import subprocess
        import sys
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.fifo"
            os.mkfifo(path)
            code = ("from bandctl.settings import read_settings\n"
                    "import sys\n"
                    "try:\n"
                    "    read_settings(sys.argv[1])\n"
                    "except ValueError as exc:\n"
                    "    print(exc)\n")
            result = subprocess.run([sys.executable, "-c", code, str(path)],
                                    capture_output=True, text=True, timeout=2, check=True)
            self.assertIn("regular file", result.stdout)

    def test_file_round_trip_is_private(self):
        import os
        import stat
        import tempfile
        from pathlib import Path
        from bandctl.settings import read_settings, write_settings

        settings = SettingsTests().settings()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("old content")
            write_settings(path, settings)
            self.assertEqual(read_settings(path), settings)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual([entry.name for entry in Path(directory).iterdir()], ["settings.json"])

    def test_failed_replace_keeps_original_and_cleans_temp_file(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from bandctl.settings import write_settings

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("old content")
            with patch("os.replace", side_effect=OSError("cannot replace")):
                with self.assertRaisesRegex(ValueError, "cannot write settings"):
                    write_settings(path, SettingsTests().settings())
            self.assertEqual(path.read_text(), "old content")
            self.assertEqual([entry.name for entry in Path(directory).iterdir()], ["settings.json"])

    def test_rejects_oversized_invalid_utf8_and_missing_files(self):
        import tempfile
        from pathlib import Path
        from bandctl.settings import read_settings

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with self.assertRaisesRegex(ValueError, "cannot read"):
                read_settings(path)
            path.write_bytes(b"x" * 1_048_577)
            with self.assertRaisesRegex(ValueError, "too large"):
                read_settings(path)
            path.write_bytes(b"\xff")
            with self.assertRaisesRegex(ValueError, "UTF-8"):
                read_settings(path)


class SettingsSessionTests(unittest.TestCase):
    def test_snapshot_keeps_only_group_members_and_start_times(self):
        from bandctl.settings import snapshot_settings
        from bandctl.shaper import Share

        boot_id = "12345678-1234-4234-8234-123456789abc"
        shares = {42: Share(42, 30, None, 10, "/", "secret arguments", 100, group="work"),
                  43: Share(43, 30, None, 10, "/", "other", 101, group="work"),
                  1: Share(1, None, 10, 11, "/", "other")}
        settings = snapshot_settings("eth0", 100, None, [], shares, boot_id)
        self.assertEqual(settings.groups, (GroupSettings(Group("work", 30, None, (42, 43)), (100, 101)),))
        self.assertNotIn("secret arguments", dumps(settings))
        shares[43].start_ticks = None
        with self.assertRaisesRegex(ValueError, "start times"):
            snapshot_settings("eth0", 100, None, [], shares, boot_id)

    def test_target_checks_are_all_read_only(self):
        from types import SimpleNamespace
        from bandctl.settings import validate_targets

        settings = SettingsTests().settings()
        boot = settings.boot_id
        valid = [SimpleNamespace(pid=42, start_ticks=100), SimpleNamespace(pid=43, start_ticks=101)]
        validate_targets(settings, "eth0", boot, valid)
        with self.assertRaisesRegex(ValueError, "interface"):
            validate_targets(settings, "wlan0", boot, valid)
        with self.assertRaisesRegex(ValueError, "different boot"):
            validate_targets(settings, "eth0", "other", valid)
        with self.assertRaisesRegex(ValueError, "pid 43"):
            validate_targets(settings, "eth0", boot, valid[:1])
        with self.assertRaisesRegex(ValueError, "pid 43"):
            validate_targets(settings, "eth0", boot, [valid[0], SimpleNamespace(pid=43, start_ticks=102)])


class GroupChangesTests(unittest.TestCase):
    def test_plans_retained_added_and_removed_members(self):
        from bandctl.settings import GroupChanges, plan_group_changes
        from bandctl.shaper import Share

        settings = SettingsTests().settings()
        shares = {42: Share(42, 30, None, 10, "/", start_ticks=100, group="old"),
                  44: Share(44, None, 10, 10, "/", start_ticks=102, group="old"),
                  1: Share(1, None, 10, 11, "/")}
        self.assertEqual(plan_group_changes(settings, shares), GroupChanges(frozenset({42}), frozenset({43}), frozenset({44})))
        self.assertEqual(set(shares), {1, 42, 44})

    def test_rejects_individual_limits_and_changed_instances(self):
        from bandctl.settings import plan_group_changes
        from bandctl.shaper import Share

        settings = SettingsTests().settings()
        with self.assertRaisesRegex(ValueError, "individual limit"):
            plan_group_changes(settings, {42: Share(42, 30, None, 10, "/", start_ticks=100)})
        with self.assertRaisesRegex(ValueError, "changed"):
            plan_group_changes(settings, {42: Share(42, 30, None, 10, "/", start_ticks=99, group="old")})
