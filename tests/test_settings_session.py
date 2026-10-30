"""Session settings against the fake shaper host."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bandctl.groups import Group
from bandctl.procscan import ProcessInfo
from bandctl.rules import AutoRule
from bandctl.settings import read_settings
from bandctl.settings_session import export_session, import_session
from bandctl.shaper import ShaperError
from test_shaper import _shaper


class SettingsSessionTests(unittest.TestCase):
    def setUp(self):
        self.shaper, self.host = _shaper()
        self.host.files["/proc/sys/kernel/random/boot_id"] = "12345678-1234-4234-8234-123456789abc"
        for pid in (42, 43, 44):
            self.host.files[f"/proc/{pid}/stat"] = f"{pid} (work) S " + "0 " * 18 + "100"
            self.host.files[f"/proc/{pid}/cgroup"] = "0::/user.slice/session.scope\n"
        self.procs = [ProcessInfo(pid, "work", "work", "user", 0, 100) for pid in (42, 43, 44)]

    def test_export_import_restores_groups_rules_and_budgets(self):
        self.shaper.apply_group(Group("work", 30, 10, (42, 43)), {42: 100, 43: 100}, 100_000_000, 100_000_000)
        rules = [AutoRule("steam", 20, None)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            export_session(path, self.shaper, "eth0", 100_000_000, None, rules)
            self.shaper.remove(42)
            self.shaper.remove(43)
            # The fake host does not remove pseudo-files when a cgroup is removed.
            for pid in (42, 43):
                self.host.files.pop(f"/sys/fs/cgroup/bandctl/bandctl-{pid}/cgroup.procs", None)
            settings = import_session(path, self.shaper, "eth0", 50_000_000, self.procs)
        self.assertEqual(settings.rules, tuple(rules))
        self.assertEqual((settings.down_budget, settings.up_budget), (100_000_000, None))
        self.assertEqual({pid: share.group for pid, share in self.shaper.shares.items()}, {42: "work", 43: "work"})
        self.assertEqual((self.shaper._down_cap, self.shaper._up_cap), (100_000_000, 50_000_000))

    def test_invalid_file_or_missing_process_changes_nothing(self):
        self.shaper.apply_group(Group("work", 30, None, (42,)), {42: 100}, 100_000_000, None)
        previous = self.shaper.shares
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            export_session(path, self.shaper, "eth0", None, None, [])
            with self.assertRaisesRegex(ValueError, "pid 42"):
                import_session(path, self.shaper, "eth0", 100_000_000, [])
            self.assertEqual(self.shaper.shares, previous)
            path.write_text('{"version": 1}')
            with self.assertRaises(ValueError):
                import_session(path, self.shaper, "eth0", 100_000_000, self.procs)
            self.assertEqual(self.shaper.shares, previous)

    def test_failure_does_not_return_new_rules_or_budget(self):
        self.shaper.apply_group(Group("work", 30, None, (42,)), {42: 100}, 100_000_000, None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            export_session(path, self.shaper, "eth0", None, None, [AutoRule("work", 10, None)])
            with patch.object(self.shaper, "replace_groups", side_effect=ShaperError("failed")):
                with self.assertRaisesRegex(ValueError, "failed"):
                    import_session(path, self.shaper, "eth0", 100_000_000, self.procs)
            self.assertEqual(read_settings(path).rules[0].name, "work")

    def test_deeply_nested_settings_are_rejected_without_changing_limits(self):
        self.shaper.apply_group(Group("work", 30, None, (42,)), {42: 100}, 100_000_000, None)
        previous = self.shaper.shares
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("[" * 20_000 + "]" * 20_000)
            with self.assertRaisesRegex(ValueError, "invalid settings JSON"):
                import_session(path, self.shaper, "eth0", 100_000_000, self.procs)
        self.assertEqual(self.shaper.shares, previous)
