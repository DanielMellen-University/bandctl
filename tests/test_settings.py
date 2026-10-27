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
                        (GroupSettings(Group("work", 30, None, (42, 43)), (100, 101)),))

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

    def test_rejects_overlapping_groups(self):
        data = json.loads(dumps(self.settings()))
        extra = json.loads(json.dumps(data["groups"][0]))
        extra["name"] = "other"
        data["groups"].append(extra)
        with self.assertRaisesRegex(ValueError, "multiple groups"):
            loads(json.dumps(data))


class SettingsFileTests(unittest.TestCase):
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
