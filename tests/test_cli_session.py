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

    def test_refresh_updates_detected_capacity_and_preserves_it_on_failure(self):
        from datetime import time
        self.session.limit("42", "30", None)
        self.session.speed_source = lambda: 50_000_000
        self.session.refresh(now=1, current=time(10))
        self.assertEqual(self.session.detected_bps, 50_000_000)
        self.assertEqual(self.shaper._down_cap, 50_000_000)
        self.session.speed_source = lambda: None
        messages = self.session.refresh(now=2, current=time(10))
        self.assertTrue(any("capacity update" in message for message in messages))
        self.assertTrue(any("capacity update" in message for message in self.session.warnings))
        self.assertEqual(self.session.detected_bps, 50_000_000)
        self.assertEqual(self.shaper._down_cap, 50_000_000)

    def test_refresh_retries_capacity_after_speed_returns_to_old_value(self):
        from datetime import time
        from unittest.mock import patch

        self.session.limit("42", "30", None)
        speed = [200_000_000]
        self.session.speed_source = lambda: speed[0]
        set_capacities = self.shaper.set_capacities
        with patch.object(self.shaper, "set_capacities", side_effect=ShaperError("rollback incomplete")):
            messages = self.session.refresh(now=1, current=time(10))
        self.assertTrue(any("rollback incomplete" in message for message in messages))
        self.assertEqual(self.session.detected_bps, 100_000_000)
        speed[0] = 100_000_000
        with patch.object(self.shaper, "set_capacities", wraps=set_capacities) as update:
            self.session.refresh(now=2, current=time(10))
            update.assert_called_once_with(100_000_000, 100_000_000)
        with patch.object(self.shaper, "set_capacities", wraps=set_capacities) as update:
            self.session.refresh(now=3, current=time(10))
            update.assert_not_called()

    def test_refresh_reports_socket_filter_failures(self):
        from datetime import time
        from unittest.mock import patch
        with patch.object(self.shaper, "_sync_flows", side_effect=ShaperError("filter failed")):
            self.session.refresh(now=1, current=time(10))
        self.assertTrue(any("filter failed" in warning for warning in self.session.warnings))

    def test_warnings_outlive_one_refresh_then_expire(self):
        from datetime import time
        from unittest.mock import patch
        with patch.object(self.shaper, "_sync_flows", side_effect=ShaperError("filter failed")):
            self.session.refresh(now=1, current=time(10))
        self.assertTrue(self.session.warnings)
        self.session.refresh(now=20, current=time(10))
        self.assertTrue(any("filter failed" in warning for warning in self.session.warnings))
        self.session.refresh(now=70, current=time(10))
        self.assertEqual(self.session.warnings, [])

    def test_repeated_warning_moves_to_most_recent_position(self):
        first = "flow refresh: first"
        second = "capacity update: second"
        self.session._remember_warnings([first], 1)
        self.session._remember_warnings([second], 2)
        self.session._remember_warnings([first], 3)
        self.assertEqual(self.session.warnings, [second, first])

    def test_refresh_failure_preserves_other_warnings_and_expires(self):
        from unittest.mock import patch

        self.session._remember_warnings(["flow refresh: filter failed"], 1)
        with patch("bandctl.cli_session.time.monotonic", return_value=2):
            self.session.record_refresh_failure(OSError("scan failed"))
        self.assertEqual(self.session.warnings, ["flow refresh: filter failed", "refresh failed: scan failed"])
        self.session._remember_warnings([], 63)
        self.assertEqual(self.session.warnings, [])

    def test_interrupted_refresh_retains_warnings_from_completed_steps(self):
        from datetime import time
        from unittest.mock import patch

        self.session.speed_source = lambda: 50_000_000
        self.session.processes = lambda: (_ for _ in ()).throw(OSError("scan failed"))
        with patch.object(self.shaper, "set_capacities", side_effect=ShaperError("capacity failed")), \
             patch.object(self.shaper, "_sync_flows", side_effect=ShaperError("filter failed")):
            with self.assertRaisesRegex(OSError, "scan failed"):
                self.session.refresh(now=1, current=time(10))
        self.assertIn("capacity update: capacity failed", self.session.warnings)
        self.assertIn("flow refresh: filter failed", self.session.warnings)

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

    def test_scheduled_rule_switches_to_fallback_and_respects_manual_override(self):
        from datetime import time
        from unittest.mock import patch
        from bandctl.schedule import DailyWindow

        self.session.rules = [
            AutoRule("firefox", 20, None, DailyWindow(time(9), time(17))),
            AutoRule("firefox", None, 10),
        ]
        self.session.refresh(now=1, current=time(10))
        self.assertEqual((self.shaper.shares[42].down_pct, self.shaper.shares[42].up_pct), (20, None))
        write = self.host.write

        def move_process(path, data):
            write(path, data)
            if path == "/sys/fs/cgroup/user.slice/user-1000.slice/session-1.scope/cgroup.procs":
                self.host.files["/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs"] = ""

        with patch.object(self.host, "write", side_effect=move_process):
            self.session.refresh(now=2, current=time(17))
        self.assertEqual((self.shaper.shares[42].down_pct, self.shaper.shares[42].up_pct), (None, 10))

        self.session.limit("42", "40", None)
        self.session.refresh(now=3, current=time(10))
        self.assertEqual((self.shaper.shares[42].down_pct, self.shaper.shares[42].up_pct), (40, None))

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
