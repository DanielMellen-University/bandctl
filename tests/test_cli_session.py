"""CLI session requests with a fake shaping host."""
import tempfile
import unittest
from pathlib import Path

from bandctl.cli_session import CliSession
from bandctl.procscan import ProcessInfo
from bandctl.rules import AutoRule
from bandctl.settings import Settings, write_settings
from bandctl.shaper import ShaperError
from test_shaper import _shaper


class CliSessionTests(unittest.TestCase):
    def setUp(self):
        self.shaper, self.host = _shaper()
        self.host.files["/proc/42/stat"] = "42 (firefox) S " + "0 " * 18 + "100"
        self.procs = [ProcessInfo(42, "firefox", "firefox --secret", "user", 0, 100)]
        self.session = CliSession(self.shaper, "eth0", 100_000_000, lambda: self.procs)

    def test_limit_and_remove_use_verified_shaper(self):
        result = self.session.limit("firefox", "30", "10")
        self.assertEqual(result, {"pid": 42, "name": "firefox", "down": 30, "up": 10})
        self.assertEqual(self.shaper.shares[42].start_ticks, 100)
        self.assertEqual(self.session.remove("42"), {"pid": 42, "name": "firefox"})
        self.assertFalse(self.shaper.shares)

    def test_limit_refuses_missing_capacity_or_changed_process(self):
        self.session.detected_bps = None
        with self.assertRaisesRegex(ShaperError, "capacity"):
            self.session.limit("42", "30", None)
        self.assertFalse(self.shaper.shares)
        self.session.detected_bps = 100_000_000
        self.host.files["/proc/42/stat"] = "42 (other) S " + "0 " * 18 + "101"
        with self.assertRaisesRegex(ShaperError, "changed"):
            self.session.limit("42", "30", None)
        self.assertFalse(self.shaper.shares)

    def test_remove_does_not_target_a_reused_pid(self):
        self.session.limit("42", "30", None)
        self.procs[0] = ProcessInfo(42, "other", "", "user", 0, 101)
        with self.assertRaisesRegex(ShaperError, "changed"):
            self.session.remove("42")
        self.assertIn(42, self.shaper.shares)

    def test_refresh_applies_and_expires_scheduled_rules(self):
        from datetime import time
        from bandctl.schedule import DailyWindow
        self.session.rules = [AutoRule("firefox", 20, None, DailyWindow(time(9), time(17)))]
        messages = self.session.refresh(now=1, current=time(10))
        self.assertTrue(any("auto rule" in message for message in messages))
        self.assertEqual(self.shaper.shares[42].down_pct, 20)
        messages = self.session.refresh(now=2, current=time(17))
        self.assertTrue(any("schedule ended" in message for message in messages))
        self.assertFalse(self.shaper.shares)

    def test_manual_remove_prevents_rule_reapplying_to_same_process(self):
        from datetime import time
        self.session.rules = [AutoRule("firefox", 20, None)]
        self.session.refresh(now=1, current=time(10))
        self.session.remove("42")
        self.session.refresh(now=2, current=time(10))
        self.assertFalse(self.shaper.shares)

    def test_settings_are_committed_only_after_import_succeeds(self):
        self.session.rules = [AutoRule("old", 10, None)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            write_settings(path, Settings("eth0", 80_000_000, None, (AutoRule("new", 20, None),), ()))
            self.session.import_file(path)
            self.assertEqual(self.session.down_budget, 80_000_000)
            self.assertEqual(self.session.rules, [AutoRule("new", 20, None)])
            path.write_text("invalid")
            with self.assertRaises(ValueError):
                self.session.import_file(path)
            self.assertEqual(self.session.down_budget, 80_000_000)
            self.assertEqual(self.session.rules, [AutoRule("new", 20, None)])
