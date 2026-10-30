"""Shaper plans and rollback against a fake host. No root, no real tc."""

from __future__ import annotations

import json
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bandctl.groups import Group
from bandctl.rates import htb_plan, percent_to_bps
from bandctl.procscan import SockFlow
from bandctl.shaper import (
    IFB,
    InstalledFlow,
    LinuxHost,
    Share,
    Shaper,
    ShaperError,
    flower_command,
    nft_script,
    parse_class_bytes,
    parse_root_qdisc,
)


class FakeHost:
    def __init__(self):
        self.commands: list[tuple[list[str], str | None]] = []
        self.files: dict[str, str] = {
            "/proc/net/tcp": "sl local_address rem_address inode\n",
            "/proc/net/udp": "sl local_address rem_address inode\n",
        }
        self.dirs: set[str] = {"/sys/fs/cgroup"}
        self.links: dict[str, str] = {}
        self.qdisc = "qdisc fq_codel 0: root refcnt 2 limit 10240p flows 1024\n"
        self.class_show: dict[str, str] = {}
        self.ifb_exists = False
        self.fail_substr: str | None = None

    def run(self, args: list[str], input_text: str | None = None) -> str:
        joined = " ".join(args)
        self.commands.append((list(args), input_text))
        if self.fail_substr and self.fail_substr in joined:
            raise ShaperError("forced failure", args)
        if args[:3] == ["tc", "qdisc", "show"]:
            return self.qdisc
        if args[:4] == ["tc", "-s", "class", "show"]:
            dev = args[args.index("dev") + 1]
            return self.class_show.get(dev, "")
        if args[:3] == ["ip", "link", "show"] and args[-1] == IFB:
            if not self.ifb_exists:
                raise ShaperError('Device "ifb-bandctl" does not exist.', args)
            return "2: ifb-bandctl: <BROADCAST> mtu 1500 qdisc htb state UNKNOWN"
        if args[:4] == ["ip", "link", "add", IFB]:
            if self.ifb_exists:
                raise ShaperError("RTNETLINK answers: File exists", args)
            self.ifb_exists = True
            return ""
        if args[:4] == ["ip", "link", "del", IFB]:
            self.ifb_exists = False
            return ""
        return ""

    def read(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def read_state(self, path: str) -> str:
        return self.read(path)

    def write(self, path: str, data: str) -> None:
        self.files[path] = data
        prefix = "/sys/fs/cgroup"
        if path.startswith(prefix + "/") and path.endswith("/cgroup.procs"):
            pid = data.strip()
            current = f"/proc/{pid}/cgroup"
            if current in self.files:
                cgroup = path[len(prefix):-len("/cgroup.procs")] or "/"
                self.files[current] = f"0::{cgroup}\n"

    def write_state(self, path: str, data: str) -> None:
        self.write(path, data)

    def mkdir(self, path: str) -> None:
        self.dirs.add(path)

    def rmdir(self, path: str) -> None:
        if path not in self.dirs:
            raise FileNotFoundError(path)
        self.dirs.remove(path)

    def listdir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        names: set[str] = set()
        for entry in list(self.dirs) + list(self.files) + list(self.links):
            if entry.startswith(prefix):
                names.add(entry[len(prefix) :].split("/")[0])
        return sorted(names)

    def readlink(self, path: str) -> str:
        if path not in self.links:
            raise FileNotFoundError(path)
        return self.links[path]

    def unlink(self, path: str) -> None:
        self.files.pop(path, None)


def _shaper(host: FakeHost | None = None) -> tuple[Shaper, FakeHost]:
    host = host or FakeHost()
    host.files["/proc/42/cgroup"] = "0::/user.slice/user-1000.slice/session-1.scope\n"
    host.files["/proc/43/cgroup"] = "0::/user.slice/user-1000.slice/session-1.scope\n"
    # State lives on the fake host, not the real /run.
    shaper = Shaper(iface="eth0", host=host, state_path="/run/bandctl.json")
    return shaper, host


def _joined(host: FakeHost) -> list[str]:
    return [" ".join(args) for args, _text in host.commands]


class ParseTests(unittest.TestCase):
    def test_command_output_with_invalid_utf8_does_not_crash(self):
        host = LinuxHost()
        output = host.run([sys.executable, "-c", "import os; os.write(1, b'out\\xff')"])
        self.assertEqual(output, "out\ufffd")
        with self.assertRaisesRegex(ShaperError, "error\ufffd"):
            host.run([sys.executable, "-c", "import os; os.write(2, b'error\\xff'); exit(1)"])

    def test_root_qdisc(self):
        self.assertEqual(parse_root_qdisc("qdisc fq_codel 0: root refcnt 2\n"), "fq_codel")
        self.assertIsNone(parse_root_qdisc("qdisc fq_codel 0: parent 1:1\n"))

    def test_class_bytes_skip_parent_and_default(self):
        text = (
            "class htb 1:1 root\n Sent 9 bytes 1 pkt\n"
            "class htb 1:999 parent 1:1\n Sent 8 bytes 1 pkt\n"
            "class htb 1:10 parent 1:1 leaf 1\n Sent 125000 bytes 80 pkt\n"
        )
        self.assertEqual(parse_class_bytes(text), {10: 125000})

    def test_unconnected_udp_flower_omits_the_remote(self):
        flow = SockFlow("udp", "ip", "10.0.0.8", 5353, "", 0)
        command = " ".join(
            flower_command(
                dev="eth0", parent="1:", prio=2, handle=1,
                flow=flow, minor=10, direction="up",
            )
        )
        self.assertIn("src_port 5353", command)
        self.assertNotIn("dst_ip", command)
        self.assertNotIn("dst_port", command)

    def test_wildcard_udp_port_matching_is_opt_in_and_family_specific(self):
        for family, address in (("ip", "0.0.0.0"), ("ipv6", "::")):
            for direction in ("up", "down"):
                flow = SockFlow("udp", family, address, 5353, "1.2.3.4" if family == "ip" else "2001:db8::1", 9999)
                arguments = dict(dev="eth0", parent="1:", prio=1, handle=1,
                                 flow=flow, minor=10, direction=direction)
                with self.subTest(family=family, direction=direction):
                    default = flower_command(**arguments)
                    self.assertIn(address, default)
                    self.assertIn("9999", default)
                    enabled = flower_command(**arguments, match_wildcard=True)
                    self.assertIn(family, enabled)
                    self.assertIn("src_port" if direction == "up" else "dst_port", enabled)
                    self.assertIn("5353", enabled)
                    self.assertNotIn("src_ip", enabled)
                    self.assertNotIn("dst_ip", enabled)
                    self.assertNotIn("9999", enabled)

    def test_wildcard_option_does_not_change_tcp_or_specific_udp(self):
        for flow in (SockFlow("tcp", "ip", "0.0.0.0", 1, "1.2.3.4", 2),
                     SockFlow("udp", "ip", "10.0.0.8", 1, "1.2.3.4", 2)):
            for direction in ("up", "down"):
                args = dict(dev="eth0", parent="1:", prio=1, handle=1, flow=flow, minor=10, direction=direction)
                with self.subTest(flow=flow, direction=direction):
                    self.assertEqual(flower_command(**args), flower_command(**args, match_wildcard=True))

    def test_nft_matches_leaf_name(self):
        script = nft_script([Share(42, 30, None, 10, "/user.slice", "curl")])
        self.assertNotIn("add table inet bandctl", script)
        self.assertIn('socket cgroupv2 level 2 "bandctl-42"', script)
        self.assertIn("ct mark set 10 meta mark set 10", script)
        self.assertIn("hook output", script)
        self.assertIn("hook prerouting", script)


class ShutdownRaceTests(unittest.TestCase):
    def test_apply_waiting_for_lock_does_not_run_after_teardown(self):
        shaper, host = _shaper()
        entered = threading.Event()
        lock = threading.RLock()

        class ObservedLock:
            def __enter__(self):
                if threading.current_thread() is worker:
                    entered.set()
                lock.acquire()

            def __exit__(self, *_args):
                lock.release()

        shaper._lock = ObservedLock()
        errors = []

        def apply():
            try:
                shaper.apply(42, None, 30, None, 100_000_000)
            except ShaperError as exc:
                errors.append(str(exc))

        worker = threading.Thread(target=apply)
        with lock:
            worker.start()
            self.assertTrue(entered.wait(2))
            shaper.teardown()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ["shaper is shut down"])
        self.assertEqual(shaper.shares, {})
        self.assertNotIn("/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs", host.files)


class SharedClassTests(unittest.TestCase):
    def group_shaper(self):
        shaper, host = _shaper()
        for pid in (42, 43):
            host.files[f"/proc/{pid}/stat"] = f"{pid} (work) S " + "0 " * 18 + "100"
        return shaper, host

    def test_install_and_remove_group_members(self):
        shaper, host = self.group_shaper()
        shaper.apply_group(Group("work", 30, 20, (42, 43)), {42: 100, 43: 100}, 100_000_000, 100_000_000)
        first, second = shaper.shares[42], shaper.shares[43]
        self.assertEqual((first.minor, first.group), (second.minor, "work"))
        with self.assertRaisesRegex(ShaperError, "group"):
            shaper.apply(42, None, 5, None, 100_000_000)
        shaper.remove(42)
        self.assertIn(43, shaper.shares)
        self.assertIn(("eth0", second.minor), shaper._classes)
        shaper.remove(43)
        self.assertFalse(shaper.shares)

    def test_add_member_uses_group_class_and_verifies_identity(self):
        shaper, host = self.group_shaper()
        shaper.apply_group(Group("work", 30, 20, (42,)), {42: 100}, 100_000_000, 100_000_000)
        with self.assertRaisesRegex(ShaperError, "changed"):
            shaper.add_group_member("WORK", 43, expected_start_ticks=101)
        self.assertNotIn(43, shaper.shares)
        shaper.add_group_member("WORK", 43, expected_start_ticks=100)
        first, second = shaper.shares[42], shaper.shares[43]
        self.assertEqual((second.minor, second.down_pct, second.up_pct, second.group),
                         (first.minor, 30, 20, "work"))
        with self.assertRaisesRegex(ShaperError, "already has"):
            shaper.add_group_member("work", 43, expected_start_ticks=100)

    def test_group_operations_report_failed_synchronization_rollback(self):
        for operation in ("create", "add", "update"):
            with self.subTest(operation=operation):
                shaper, host = self.group_shaper()
                if operation != "create":
                    shaper.apply_group(Group("work", None, 20, (42,)), {42: 100}, None, 100_000_000)
                with patch.object(shaper, "_sync", side_effect=[ShaperError("update failed"),
                                                               ShaperError("rollback failed")]):
                    with self.assertRaisesRegex(ShaperError, "update failed.*rollback incomplete.*rollback failed"):
                        if operation == "create":
                            shaper.apply_group(Group("work", None, 20, (42,)), {42: 100}, None, 100_000_000)
                        elif operation == "add":
                            shaper.add_group_member("work", 43, expected_start_ticks=100)
                        else:
                            shaper.update_group("work", None, 30, None, 100_000_000)

    def test_group_add_reports_failed_process_restoration(self):
        shaper, host = self.group_shaper()
        shaper.apply_group(Group("work", None, 20, (42,)), {42: 100}, None, 100_000_000)
        with patch.object(shaper, "_restore_share", side_effect=ShaperError("restore failed")), \
             patch.object(shaper, "_sync", side_effect=[ShaperError("update failed"), None]):
            with self.assertRaisesRegex(ShaperError, "rollback incomplete.*restore failed"):
                shaper.add_group_member("work", 43, expected_start_ticks=100)
        self.assertIn(43, shaper.shares)

    def test_failed_add_restores_member_and_preserves_group(self):
        shaper, host = self.group_shaper()
        shaper.apply_group(Group("work", None, 20, (42,)), {42: 100}, None, 100_000_000)
        with patch.object(shaper, "_sync", side_effect=[ShaperError("forced"), None]):
            with self.assertRaisesRegex(ShaperError, "forced"):
                shaper.add_group_member("work", 43, expected_start_ticks=100)
        self.assertEqual(set(shaper.shares), {42})
        self.assertNotIn("/sys/fs/cgroup/bandctl/bandctl-43", host.dirs)

    def test_failed_add_keeps_member_tracked_when_restore_fails(self):
        shaper, host = self.group_shaper()
        shaper.apply_group(Group("work", None, 20, (42,)), {42: 100}, None, 100_000_000)
        with patch.object(shaper, "_restore_share", side_effect=ShaperError("restore failed")):
            with patch.object(shaper, "_sync", side_effect=[ShaperError("forced"), None]):
                with self.assertRaisesRegex(ShaperError, "forced"):
                    shaper.add_group_member("work", 43, expected_start_ticks=100)
        self.assertEqual(set(shaper.shares), {42, 43})
        self.assertEqual(shaper.shares[43].group, "work")

    def test_group_update_changes_all_members_and_rolls_back_on_error(self):
        shaper, host = self.group_shaper()
        shaper.apply_group(Group("work", None, 20, (42, 43)), {42: 100, 43: 100}, None, 100_000_000)
        old_revisions = [shaper.shares[pid].revision for pid in (42, 43)]
        shaper.update_group("WORK", 30, 40, 100_000_000, 100_000_000)
        for pid, revision in zip((42, 43), old_revisions):
            share = shaper.shares[pid]
            self.assertEqual((share.down_pct, share.up_pct), (30, 40))
            self.assertNotEqual(share.revision, revision)
        with patch.object(shaper, "_sync", side_effect=[ShaperError("forced"), None]):
            with self.assertRaisesRegex(ShaperError, "forced"):
                shaper.update_group("work", None, 10, None, 100_000_000)
        self.assertEqual([(shaper.shares[pid].down_pct, shaper.shares[pid].up_pct) for pid in (42, 43)],
                         [(30, 40), (30, 40)])
        self.assertEqual((shaper._down_cap, shaper._up_cap), (100_000_000, 100_000_000))

    def test_group_changes_keep_capacities_needed_by_other_shares(self):
        shaper, host = self.group_shaper()
        shaper.apply(42, 10, None, 100_000_000, None)
        with self.assertRaisesRegex(ShaperError, "download capacity"):
            shaper.apply_group(Group("work", None, 20, (43,)), {43: 100}, None, 100_000_000)
        self.assertEqual(set(shaper.shares), {42})
        shaper.apply_group(Group("work", None, 20, (43,)), {43: 100}, 100_000_000, 100_000_000)
        with self.assertRaisesRegex(ShaperError, "download capacity"):
            shaper.update_group("work", None, 30, None, 100_000_000)
        self.assertEqual(shaper.shares[43].up_pct, 20)
        self.assertEqual(shaper._down_cap, 100_000_000)

    def test_group_preflight_rejects_reused_or_limited_pid(self):
        shaper, host = self.group_shaper()
        group = Group("work", None, 20, (42, 43))
        with self.assertRaisesRegex(ShaperError, "changed"):
            shaper.apply_group(group, {42: 100, 43: 101}, None, 100_000_000)
        self.assertFalse(shaper.shares)
        self.assertEqual(host.commands, [])
        shaper.apply(42, None, 10, None, 100_000_000)
        with self.assertRaisesRegex(ShaperError, "already has"):
            shaper.apply_group(group, {42: 100, 43: 100}, None, 100_000_000)
        self.assertEqual(set(shaper.shares), {42})

    def test_group_move_failure_restores_member_that_may_have_moved(self):
        shaper, host = self.group_shaper()
        original_move = shaper._move_to_leaf

        def fail_after_move(share):
            original_move(share)
            if share.pid == 43:
                raise ShaperError("move failed")

        with patch.object(shaper, "_move_to_leaf", side_effect=fail_after_move):
            with self.assertRaisesRegex(ShaperError, "move failed"):
                shaper.apply_group(Group("work", None, 20, (42, 43)), {42: 100, 43: 100}, None, 100_000_000)
        self.assertFalse(shaper.shares)
        destination = "/sys/fs/cgroup/user.slice/user-1000.slice/session-1.scope/cgroup.procs"
        self.assertEqual(host.files[destination], "42\n")
        self.assertNotIn("/sys/fs/cgroup/bandctl/bandctl-43", host.dirs)

    def test_group_move_failure_keeps_member_if_restore_fails(self):
        shaper, host = self.group_shaper()
        original_move = shaper._move_to_leaf

        def fail_after_move(share):
            original_move(share)
            raise ShaperError("move failed")

        with patch.object(shaper, "_move_to_leaf", side_effect=fail_after_move), \
             patch.object(shaper, "_restore_share", side_effect=ShaperError("restore failed")):
            with self.assertRaisesRegex(ShaperError, "move failed"):
                shaper.apply_group(Group("work", None, 20, (42,)), {42: 100}, None, 100_000_000)
        self.assertEqual(set(shaper.shares), {42})
        self.assertEqual(shaper.shares[42].group, "work")

    def test_group_sync_failure_restores_all_members(self):
        shaper, host = self.group_shaper()
        with patch.object(shaper, "_sync", side_effect=[ShaperError("forced"), None]):
            with self.assertRaisesRegex(ShaperError, "forced"):
                shaper.apply_group(Group("work", None, 20, (42, 43)), {42: 100, 43: 100}, None, 100_000_000)
        self.assertFalse(shaper.shares)
        self.assertNotIn("/sys/fs/cgroup/bandctl/bandctl-42", host.dirs)
        self.assertNotIn("/sys/fs/cgroup/bandctl/bandctl-43", host.dirs)

    def test_members_share_one_class_and_one_ceiling(self):
        shaper, host = _shaper()
        shares = [Share(42, None, 60, 10, "/"), Share(43, None, 60, 10, "/")]
        shaper._sync_direction("eth0", shares, 100_000_000)
        classes = [line for line in _joined(host) if "classid 1:10 " in line]
        self.assertEqual(len(classes), 1)
        self.assertIn("rate 60000kbit ceil 60000kbit", classes[0])
        script = nft_script(shares)
        self.assertIn('"bandctl-42" ct mark set 10', script)
        self.assertIn('"bandctl-43" ct mark set 10', script)

    def test_conflicting_members_fail_before_changing_classes(self):
        shaper, host = _shaper()
        shares = [Share(42, None, 10, 10, "/"), Share(43, None, 20, 10, "/")]
        with self.assertRaisesRegex(ShaperError, "conflicting"):
            shaper._sync_direction("eth0", shares, 100_000_000)
        self.assertEqual(host.commands, [])

    def test_shared_counter_is_sampled_once_for_all_members(self):
        shaper, host = _shaper()
        shaper._shares = {42: Share(42, None, 10, 10, "/"),
                          43: Share(43, None, 10, 10, "/")}
        shaper._egress = True
        with patch.object(shaper, "_sync_flows"), patch.object(shaper, "_class_bytes", return_value={10: 1000}), \
             patch("bandctl.shaper.time.monotonic", return_value=2.0):
            shaper._bytes[("up", 10)] = (500, 1.0)
            self.assertEqual(shaper.sample(), {42: (0.0, 4000.0), 43: (0.0, 4000.0)})


class ImportGroupTests(unittest.TestCase):
    BOOT = "12345678-1234-4234-8234-123456789abc"

    def setup_shaper(self):
        shaper, host = _shaper()
        host.files["/proc/sys/kernel/random/boot_id"] = self.BOOT
        for pid in (42, 43, 44):
            host.files[f"/proc/{pid}/stat"] = f"{pid} (work) S " + "0 " * 18 + "100"
            host.files[f"/proc/{pid}/cgroup"] = "0::/user.slice/session.scope\n"
        return shaper, host

    def settings(self, groups):
        from bandctl.settings import GroupSettings, Settings
        items = tuple(GroupSettings(group, tuple(100 for _ in group.members)) for group in groups)
        return Settings("eth0", None, None, (), items, self.BOOT if items else None)

    def test_replace_groups_keeps_adds_and_removes_members(self):
        shaper, host = self.setup_shaper()
        shaper.apply_group(Group("old", 20, None, (42, 44)), {42: 100, 44: 100}, 100_000_000, None)
        settings = self.settings([Group("new", 30, 10, (42, 43))])
        shaper.replace_groups(settings, 100_000_000, 100_000_000)
        self.assertEqual(set(shaper.shares), {42, 43})
        self.assertEqual(shaper.shares[42].group, "new")
        self.assertEqual(shaper.shares[42].minor, shaper.shares[43].minor)
        self.assertEqual((shaper.shares[42].down_pct, shaper.shares[43].up_pct), (30, 10))
        self.assertNotIn("/sys/fs/cgroup/bandctl/bandctl-44", host.dirs)
        self.assertEqual(host.files["/sys/fs/cgroup/user.slice/session.scope/cgroup.procs"], "44\n")

    def test_failed_import_does_not_move_a_member_that_left_its_leaf(self):
        shaper, host = self.setup_shaper()
        shaper.apply_group(Group("old", 20, None, (42,)), {42: 100}, 100_000_000, None)
        host.files["/proc/42/cgroup"] = "0::/new.slice\n"
        with patch.object(shaper, "_sync", side_effect=[ShaperError("sync failed"), None]):
            with self.assertRaisesRegex(ShaperError, "sync failed"):
                shaper.replace_groups(self.settings([]), 100_000_000, None)
        self.assertEqual(host.files["/proc/42/cgroup"], "0::/new.slice\n")
        self.assertIn(42, shaper.shares)

    def test_import_rollback_does_not_move_a_member_after_a_failed_cgroup_read(self):
        shaper, host = self.setup_shaper()
        shaper.apply_group(Group("old", 20, None, (42,)), {42: 100}, 100_000_000, None)
        host.files["/proc/42/cgroup"] = "0::/new.slice\n"
        read = host.read
        failed = False

        def fail_once(path):
            nonlocal failed
            if path == "/proc/42/cgroup" and not failed:
                failed = True
                raise PermissionError("cgroup read denied")
            return read(path)

        with patch.object(host, "read", side_effect=fail_once):
            with self.assertRaisesRegex(ShaperError, "cgroup read denied"):
                shaper.replace_groups(self.settings([]), 100_000_000, None)
        self.assertEqual(host.files["/proc/42/cgroup"], "0::/new.slice\n")
        self.assertIn(42, shaper.shares)

    def test_preflight_rejects_reused_pid_and_individual_limits(self):
        shaper, host = self.setup_shaper()
        settings = self.settings([Group("new", 30, None, (42, 43))])
        host.files["/proc/43/stat"] = "43 (new) S " + "0 " * 18 + "101"
        with self.assertRaisesRegex(ShaperError, "changed"):
            shaper.replace_groups(settings, 100_000_000, None)
        self.assertFalse(shaper.shares)
        self.assertEqual(host.commands, [])
        host.files["/proc/43/stat"] = "43 (work) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 10, None, 100_000_000)
        with self.assertRaisesRegex(ShaperError, "individual"):
            shaper.replace_groups(settings, 100_000_000, 100_000_000)
        self.assertIsNone(shaper.shares[42].group)

    def test_preflight_rejects_wrong_boot_and_unknown_capacity(self):
        shaper, host = self.setup_shaper()
        settings = self.settings([Group("new", 30, None, (42,))])
        host.files["/proc/sys/kernel/random/boot_id"] = "different"
        with self.assertRaisesRegex(ShaperError, "different boot"):
            shaper.replace_groups(settings, 100_000_000, None)
        host.files["/proc/sys/kernel/random/boot_id"] = self.BOOT
        with self.assertRaisesRegex(ShaperError, "capacity"):
            shaper.replace_groups(settings, None, None)
        self.assertFalse(shaper.shares)

    def test_invalid_rate_import_does_not_move_any_group_member(self):
        shaper, host = self.setup_shaper()
        shaper.apply_group(Group("old", None, 20, (42,)), {42: 100}, None, 100_000_000)
        previous = shaper.shares
        settings = self.settings([Group("new", None, 30, (43,))])
        with patch.object(shaper, "_move_to_leaf", side_effect=ShaperError("unexpected move")) as move, \
             patch.object(shaper, "_restore_share", side_effect=ShaperError("unexpected restore")) as restore:
            with self.assertRaisesRegex(ShaperError, "too small"):
                shaper.replace_groups(settings, None, 1)
        move.assert_not_called()
        restore.assert_not_called()
        self.assertEqual(shaper.shares, previous)
        self.assertEqual(shaper._up_cap, 100_000_000)

    def test_invalid_group_rate_does_not_move_any_member(self):
        shaper, host = self.setup_shaper()
        with patch.object(shaper, "_move_to_leaf", side_effect=ShaperError("unexpected move")) as move:
            with self.assertRaisesRegex(ShaperError, "too small"):
                shaper.apply_group(Group("new", None, 10, (42, 43)), {42: 100, 43: 100}, None, 1)
        move.assert_not_called()
        self.assertFalse(shaper.shares)

    def test_invalid_group_update_does_not_resync_existing_limit(self):
        shaper, host = self.setup_shaper()
        shaper.apply_group(Group("work", None, 20, (42, 43)), {42: 100, 43: 100}, None, 100_000_000)
        previous = shaper.shares
        with patch.object(host, "run", side_effect=ShaperError("unexpected sync")) as run:
            with self.assertRaisesRegex(ShaperError, "too small") as error:
                shaper.update_group("work", None, 20, None, 1)
        self.assertNotIn("rollback incomplete", str(error.exception))
        run.assert_not_called()
        self.assertEqual(shaper.shares, previous)
        self.assertEqual(shaper._up_cap, 100_000_000)

    def test_group_does_not_move_pid_reused_after_preflight(self):
        shaper, host = self.setup_shaper()
        original_write = host.write

        def replace_second_member(path, data):
            original_write(path, data)
            if path.endswith("/bandctl-42/cgroup.procs"):
                host.files["/proc/43/stat"] = "43 (other) S " + "0 " * 18 + "200"
                host.files["/proc/43/cgroup"] = "0::/other.slice\n"

        with patch.object(host, "write", side_effect=replace_second_member) as write:
            with self.assertRaisesRegex(ShaperError, "pid 43"):
                shaper.apply_group(Group("work", None, 20, (42, 43)), {42: 100, 43: 100}, None, 100_000_000)
        self.assertFalse(any(call.args[0].endswith("/bandctl-43/cgroup.procs") for call in write.call_args_list))
        self.assertFalse(shaper.shares)

    def test_sync_failure_restores_previous_groups_and_processes(self):
        shaper, host = self.setup_shaper()
        shaper.apply_group(Group("old", 20, None, (42, 44)), {42: 100, 44: 100}, 100_000_000, None)
        previous = shaper.shares
        with patch.object(shaper, "_sync", side_effect=[ShaperError("forced"), None]):
            with self.assertRaisesRegex(ShaperError, "forced"):
                shaper.replace_groups(self.settings([Group("new", 30, None, (42, 43))]), 100_000_000, None)
        self.assertEqual(shaper.shares, previous)
        self.assertEqual(host.files["/sys/fs/cgroup/bandctl/bandctl-44/cgroup.procs"], "44\n")
        self.assertNotIn("/sys/fs/cgroup/bandctl/bandctl-43", host.dirs)

    def test_rollback_failure_reports_and_tracks_member(self):
        shaper, host = self.setup_shaper()
        settings = self.settings([Group("new", 30, None, (42,))])
        with patch.object(shaper, "_sync", side_effect=[ShaperError("forced"), None]), \
             patch.object(shaper, "_restore_share", side_effect=ShaperError("restore failed")):
            with self.assertRaisesRegex(ShaperError, "rollback incomplete"):
                shaper.replace_groups(settings, 100_000_000, None)
        self.assertEqual(shaper.shares[42].group, "new")
        self.assertEqual(shaper._down_cap, 100_000_000)

    def test_import_rollback_keeps_reused_pid_in_old_leaf(self):
        shaper, host = self.setup_shaper()
        shaper.apply_group(Group("old", 20, None, (42, 43)), {42: 100, 43: 100}, 100_000_000, None)
        host.files["/proc/42/stat"] = "42 (other) S " + "0 " * 18 + "200"
        host.files["/proc/42/cgroup"] = "0::/bandctl/bandctl-42\n"
        host.files["/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs"] = "42\n"

        with self.assertRaisesRegex(ShaperError, "cannot verify pid 42"):
            shaper.replace_groups(self.settings([Group("new", 30, None, (43,))]), 100_000_000, None)

        self.assertIn(42, shaper.shares)
        self.assertIn("42", json.loads(host.files["/run/bandctl.json"])["pids"])
        with patch.object(host, "write", wraps=host.write) as write:
            notes = shaper.teardown()
        self.assertTrue(any("cannot verify pid 42" in note for note in notes))
        self.assertFalse(any(call.args[1] == "42\n" for call in write.call_args_list))
        self.assertIn("/run/bandctl.json", host.files)

    def test_import_rollback_keeps_old_capacity_when_new_members_were_restored(self):
        shaper, host = self.setup_shaper()
        shaper.apply_group(Group("old", None, 20, (42,)), {42: 100}, None, 100_000_000)
        settings = self.settings([Group("new", None, 30, (43,))])
        move = shaper._move_to_leaf
        sync = shaper._sync
        sync_attempts = []

        def fail_old_move(share):
            if share.pid == 42:
                raise ShaperError("old member move failed")
            return move(share)

        def fail_first_sync():
            sync_attempts.append(True)
            if len(sync_attempts) == 1:
                raise ShaperError("import failed")
            return sync()

        with patch.object(shaper, "_move_to_leaf", side_effect=fail_old_move), \
             patch.object(shaper, "_sync", side_effect=fail_first_sync):
            with self.assertRaisesRegex(ShaperError, "rollback incomplete.*old member move failed"):
                shaper.replace_groups(settings, None, 200_000_000)

        self.assertEqual(set(shaper.shares), {42})
        self.assertEqual(shaper._up_cap, 100_000_000)
        classes = [cmd for cmd in _joined(host) if "class replace dev eth0" in cmd and "classid 1:10" in cmd]
        self.assertIn("ceil 20000kbit", classes[-1])


class ApplyTests(unittest.TestCase):
    def test_apply_rechecks_identity_after_creating_cgroup(self):
        shaper, host = _shaper()
        host.files["/proc/42/stat"] = "42 (work) S " + "0 " * 18 + "100"
        original_mkdir = host.mkdir

        def replace_after_mkdir(path):
            original_mkdir(path)
            if path.endswith("/bandctl-42"):
                host.files["/proc/42/stat"] = "42 (other) S " + "0 " * 18 + "200"
                host.files["/proc/42/cgroup"] = "0::/other.slice\n"

        with patch.object(host, "mkdir", side_effect=replace_after_mkdir), \
             patch.object(host, "write", wraps=host.write) as write:
            with self.assertRaisesRegex(ShaperError, "pid 42"):
                shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        self.assertFalse(any(call.args[0].endswith("/bandctl-42/cgroup.procs") for call in write.call_args_list))
        self.assertFalse(shaper.shares)

    def test_class_ids_wrap_and_skip_active_and_stale_classes(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        shaper._classes.add(("eth0", 11))
        shaper._filters.add(("eth0", 12, "ip"))
        shaper._flows[("stale",)] = InstalledFlow(1, 13, "eth0", "1:", "ip", 2)
        shaper._bytes[("up", 14)] = (9000, 1.0)
        shaper._bytes[("down", 14)] = (8000, 1.0)
        shaper._next_minor = 10_000
        shaper.apply(43, None, 10, None, 100_000_000)
        self.assertEqual(shaper.shares[42].minor, 10)
        self.assertEqual(shaper.shares[43].minor, 14)
        self.assertNotIn(("up", 14), shaper._bytes)
        self.assertNotIn(("down", 14), shaper._bytes)

    def test_class_id_exhaustion_reports_an_error(self):
        shaper, host = _shaper()
        shaper._shares = {minor: Share(minor, None, 10, minor, "/")
                          for minor in range(10, 10_000) if minor != 999}
        with self.assertRaisesRegex(ShaperError, "class IDs"):
            shaper._alloc_minor()

    def test_class_ids_reserved_for_a_group_import_cannot_be_reused(self):
        shaper, host = _shaper()
        shaper._shares = {minor: Share(minor, None, 10, minor, "/")
                          for minor in range(10, 9999) if minor != 999}
        shaper._next_minor = 9999
        minor = shaper._alloc_minor()
        self.assertEqual(minor, 9999)
        with self.assertRaisesRegex(ShaperError, "class IDs"):
            shaper._alloc_minor({minor})

    def test_invalid_individual_rate_does_not_move_process(self):
        shaper, host = _shaper()
        with patch.object(shaper, "_move_to_leaf", side_effect=ShaperError("unexpected move")) as move:
            with self.assertRaisesRegex(ShaperError, "too small"):
                shaper.apply(42, None, 10, None, 1)
        move.assert_not_called()
        self.assertFalse(shaper.shares)

    def test_invalid_individual_update_does_not_resync_existing_limit(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        previous = shaper.shares
        with patch.object(host, "run", side_effect=ShaperError("unexpected sync")) as run:
            with self.assertRaisesRegex(ShaperError, "too small"):
                shaper.apply(42, None, 10, None, 1)
        run.assert_not_called()
        self.assertEqual(shaper.shares, previous)
        self.assertEqual(shaper._up_cap, 100_000_000)

    def test_unrepresentable_share_is_rejected_and_restored(self):
        shaper, host = _shaper()
        with self.assertRaisesRegex(ShaperError, "too small"):
            shaper.apply(42, None, 10, None, 1)
        self.assertFalse(shaper.shares)
        self.assertNotIn("/sys/fs/cgroup/bandctl/bandctl-42", host.dirs)
        self.assertFalse(host.commands)

    def test_capacity_update_rejects_too_few_class_rates(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 50, None, 100_000_000)
        shaper.apply(43, None, 50, None, 100_000_000)
        with self.assertRaisesRegex(ShaperError, "too small"):
            shaper.set_capacities(None, 2000)
        self.assertEqual(shaper._up_cap, 100_000_000)
        self.assertEqual(set(shaper.shares), {42, 43})

    def test_invalid_capacity_update_does_not_resync_existing_classes(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        with patch.object(host, "run", side_effect=ShaperError("unexpected sync")) as run:
            with self.assertRaisesRegex(ShaperError, "too small") as error:
                shaper.set_capacities(None, 1)
        self.assertNotIn("rollback incomplete", str(error.exception))
        run.assert_not_called()
        self.assertEqual(shaper._up_cap, 100_000_000)
        self.assertEqual(shaper.shares[42].up_pct, 10)

    def test_individual_update_reports_failed_synchronization_rollback(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        with patch.object(shaper, "_sync", side_effect=[ShaperError("update failed"),
                                                       ShaperError("rollback failed")]):
            with self.assertRaisesRegex(ShaperError, "update failed.*rollback incomplete.*rollback failed"):
                shaper.apply(42, None, 20, None, 100_000_000)
        self.assertEqual(shaper.shares[42].up_pct, 10)

    def test_first_share_needs_capacity_before_moving_process(self):
        for down, up, down_cap, up_cap in ((30, None, None, None), (None, 10, None, 0), (30, 10, 100_000_000, None)):
            with self.subTest(down=down, up=up):
                shaper, host = _shaper()
                with self.assertRaisesRegex(ShaperError, "capacity is unknown"):
                    shaper.apply(42, down, up, down_cap, up_cap)
                self.assertFalse(shaper.shares)
                self.assertFalse(host.commands)
                self.assertNotIn("/sys/fs/cgroup/bandctl/bandctl-42", host.dirs)
                self.assertNotIn("/run/bandctl.json", host.files)

    def test_failed_capacity_update_restores_already_changed_classes(self):
        shaper, host = _shaper()
        shaper.apply(42, 10, 10, 100_000_000, 100_000_000)
        host.commands.clear()
        run = host.run
        failed = [False]

        def fail_download_class(args, input_text=None):
            if (not failed[0] and args[:3] == ["tc", "class", "replace"]
                    and "ifb-bandctl" in args and "1:10" in args):
                failed[0] = True
                raise ShaperError("download update failed", args)
            return run(args, input_text)

        with patch.object(host, "run", side_effect=fail_download_class):
            with self.assertRaisesRegex(ShaperError, "download update failed"):
                shaper.set_capacities(200_000_000, 200_000_000)

        self.assertEqual((shaper._down_cap, shaper._up_cap), (100_000_000, 100_000_000))
        up_classes = [cmd for cmd in _joined(host) if "class replace dev eth0" in cmd
                      and "classid 1:10" in cmd]
        self.assertEqual(len(up_classes), 2)
        self.assertIn("ceil 20000kbit", up_classes[0])
        self.assertIn("ceil 10000kbit", up_classes[1])

    def test_capacity_update_reports_incomplete_rollback(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        with patch.object(shaper, "_sync", side_effect=[ShaperError("update failed"),
                                                       ShaperError("rollback failed")]):
            with self.assertRaisesRegex(ShaperError, "rollback incomplete.*rollback failed"):
                shaper.set_capacities(None, 200_000_000)

    def test_failed_cgroup_lookup_keeps_previous_capacity(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        del host.files["/proc/43/cgroup"]

        with self.assertRaisesRegex(ShaperError, "pid 43"):
            shaper.apply(43, None, 20, None, 200_000_000)

        self.assertEqual(shaper._up_cap, 100_000_000)
        host.commands.clear()
        shaper._sync()
        self.assertTrue(any("classid 1:10" in cmd and "ceil 10000kbit" in cmd
                            for cmd in _joined(host)))

    def test_shares_snapshot_does_not_change_when_live_share_changes(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        snapshot = shaper.shares
        shaper.apply(42, None, 20, None, 100_000_000)
        self.assertEqual(snapshot[42].up_pct, 10)
        snapshot[42].up_pct = 99
        self.assertEqual(shaper.shares[42].up_pct, 20)

    def test_conditional_apply_preserves_an_existing_limit(self):
        shaper, host = _shaper()
        message, revision = shaper.apply_tracked(42, None, 10, None, 100_000_000)
        self.assertIn("10%", message)
        self.assertEqual(shaper.shares[42].revision, revision)
        with patch.object(host, "write", wraps=host.write) as write:
            with self.assertRaises(ShaperError):
                shaper.apply(42, None, 30, None, 100_000_000, only_if_unlimited=True)
            write.assert_not_called()
        self.assertEqual(shaper.shares[42].up_pct, 10)
        self.assertEqual(shaper.shares[42].revision, revision)

    def test_conditional_remove_refuses_a_changed_limit(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        old_revision = shaper.shares[42].revision
        shaper.apply(42, None, 10, None, 100_000_000)
        self.assertNotEqual(shaper.shares[42].revision, old_revision)
        with patch.object(host, "write", wraps=host.write) as write:
            with self.assertRaises(ShaperError):
                shaper.remove(42, expected_revision=old_revision)
            write.assert_not_called()
        self.assertIn(42, shaper.shares)
        shaper.remove(42, expected_revision=shaper.shares[42].revision)
        self.assertNotIn(42, shaper.shares)

    def test_new_share_refuses_an_occupied_old_leaf(self):
        shaper, host = _shaper()
        leaf = "/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs"
        host.files[leaf] = "99\n"
        with self.assertRaises(ShaperError) as caught:
            shaper.apply(42, None, 10, None, 100_000_000)
        self.assertIn("still contains processes", str(caught.exception))
        self.assertEqual(host.files[leaf], "99\n")
        self.assertEqual(host.commands, [])
        self.assertFalse(shaper.shares)

    def test_remove_does_not_move_reused_pid_even_when_sync_fails(self):
        shaper, host = _shaper()
        stat = "/proc/42/stat"
        host.files[stat] = "42 (steam) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        host.files[stat] = "42 (other) S " + "0 " * 18 + "200"
        host.files["/proc/42/cgroup"] = "0::/other.slice\n"
        with patch.object(shaper, "_sync", side_effect=ShaperError("sync failed")):
            with patch.object(host, "write", wraps=host.write) as write:
                with self.assertRaises(ShaperError):
                    shaper.remove(42)
                write.assert_not_called()
        self.assertIn(42, shaper.shares)

    def test_failed_remove_resynchronizes_the_restored_share(self):
        shaper, _host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        with patch.object(shaper, "_sync", side_effect=[ShaperError("remove failed"), None]) as sync:
            with self.assertRaisesRegex(ShaperError, "remove failed"):
                shaper.remove(42)
        self.assertEqual(sync.call_count, 2)
        self.assertIn(42, shaper.shares)

    def test_remove_reports_class_or_mark_filter_deletion_failure(self):
        for failed_command in ("tc filter del dev eth0 parent 1: protocol ipv6", "tc class del dev eth0"):
            with self.subTest(command=failed_command):
                shaper, host = _shaper()
                shaper.apply(42, None, 10, None, 100_000_000)
                shaper.apply(43, None, 20, None, 100_000_000)
                minor = shaper.shares[42].minor
                run = host.run
                failed = []

                def fail_once(args, input_text=None):
                    if failed_command in " ".join(args) and not failed:
                        failed.append(True)
                        raise ShaperError("deletion failed", args)
                    return run(args, input_text)

                with patch.object(host, "run", side_effect=fail_once):
                    with self.assertRaisesRegex(ShaperError, "deletion failed"):
                        shaper.remove(42)
                self.assertIn(42, shaper.shares)
                self.assertIn(("eth0", minor), shaper._classes)
                self.assertIn(("eth0", minor, "ip"), shaper._filters)
                self.assertIn(("eth0", minor, "ipv6"), shaper._filters)
                shaper.remove(42)
                self.assertNotIn(42, shaper.shares)
                self.assertNotIn(("eth0", minor), shaper._classes)

    def test_remove_accepts_an_already_removed_class_and_filters(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        shaper.apply(43, None, 20, None, 100_000_000)
        minor = shaper.shares[42].minor
        run = host.run

        def missing_entries(args, input_text=None):
            if args[:3] in (["tc", "filter", "del"], ["tc", "class", "del"]):
                raise ShaperError("RTNETLINK answers: No such file or directory", args)
            return run(args, input_text)

        with patch.object(host, "run", side_effect=missing_entries):
            shaper.remove(42)
        self.assertNotIn(42, shaper.shares)
        self.assertNotIn(("eth0", minor), shaper._classes)
        self.assertNotIn(("eth0", minor, "ip"), shaper._filters)
        self.assertNotIn(("eth0", minor, "ipv6"), shaper._filters)

    def test_failed_remove_reports_incomplete_rollback(self):
        for failure in ("move", "sync"):
            with self.subTest(failure=failure):
                shaper, _host = _shaper()
                shaper.apply(42, None, 10, None, 100_000_000)
                moves = OSError("move failed") if failure == "move" else None
                syncs = [ShaperError("remove failed"), ShaperError("sync failed") if failure == "sync" else None]
                with patch.object(shaper, "_move_to_leaf", side_effect=moves), \
                     patch.object(shaper, "_sync", side_effect=syncs):
                    with self.assertRaisesRegex(ShaperError, "remove failed.*rollback incomplete.*" + failure + " failed"):
                        shaper.remove(42)
                self.assertIn(42, shaper.shares)

    def test_sample_retires_reused_pid_without_moving_replacement(self):
        shaper, host = _shaper()
        stat = "/proc/42/stat"
        host.files[stat] = "42 (steam) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        host.files[stat] = "42 (other) S " + "0 " * 18 + "200"
        host.files["/proc/42/cgroup"] = "0::/other.slice\n"
        original = "/sys/fs/cgroup/user.slice/user-1000.slice/session-1.scope/cgroup.procs"
        self.assertNotIn(original, host.files)
        shaper.sample()
        self.assertNotIn(42, shaper.shares)
        self.assertNotIn(original, host.files)
        self.assertFalse(shaper.upload_active)

    def test_reused_pid_in_leaf_is_not_moved_by_cleanup(self):
        for operation in ("teardown", "remove", "sample"):
            with self.subTest(operation=operation):
                shaper, host = _shaper()
                host.files["/proc/42/stat"] = "42 (curl) S " + "0 " * 18 + "100"
                shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
                shaper.apply(43, None, 10, None, 100_000_000)
                host.files["/proc/42/stat"] = "42 (other) S " + "0 " * 18 + "200"
                host.files["/proc/42/cgroup"] = "0::/bandctl/bandctl-42\n"
                host.files["/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs"] = "42\n"

                with patch.object(host, "write", wraps=host.write) as write:
                    if operation == "remove":
                        with self.assertRaisesRegex(ShaperError, "cannot verify pid 42"):
                            shaper.remove(42)
                    elif operation == "sample":
                        shaper.sample()
                        self.assertIn("cannot verify pid 42", shaper.flow_warning)
                    notes = shaper.teardown()

                self.assertTrue(any("cannot verify pid 42" in note for note in notes))
                self.assertFalse(any(call.args[1] == "42\n" for call in write.call_args_list))
                self.assertIn("/run/bandctl.json", host.files)
                self.assertIn("42", json.loads(host.files["/run/bandctl.json"])["pids"])
    def test_sample_retires_exited_process(self):
        shaper, host = _shaper()
        stat = "/proc/42/stat"
        host.files[stat] = "42 (steam) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        host.files.pop(stat)
        host.files.pop("/proc/42/cgroup")
        shaper.sample()
        self.assertNotIn(42, shaper.shares)
        self.assertFalse(shaper.upload_active)

    def test_failed_retirement_restores_previous_class_plan(self):
        shaper, host = _shaper()
        host.files["/proc/42/stat"] = "42 (work) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 60, None, 100_000_000, expected_start_ticks=100)
        shaper.apply(43, None, 60, None, 100_000_000)
        host.files.pop("/proc/42/stat")
        host.files.pop("/proc/42/cgroup")
        host.commands.clear()
        run = host.run
        listdir = host.listdir
        failed = [False]

        def fail_once(args, input_text=None):
            if not failed[0] and args[:3] == ["tc", "filter", "del"] and "10" in args:
                failed[0] = True
                raise ShaperError("class filter deletion failed")
            return run(args, input_text)

        def list_live(path):
            if path == "/proc/42/fd":
                raise FileNotFoundError("process exited")
            return listdir(path)

        with patch.object(host, "run", side_effect=fail_once), \
             patch.object(host, "listdir", side_effect=list_live):
            shaper.sample()
        self.assertIn("class filter deletion failed", shaper.flow_warning)
        self.assertNotIn("rollback incomplete", shaper.flow_warning)
        self.assertEqual(set(shaper.shares), {42, 43})
        defaults = [cmd for cmd in _joined(host) if "classid 1:999" in cmd]
        self.assertIn("rate 1kbit", defaults[-1])
        shaper.sample()
        self.assertNotIn(42, shaper.shares)
        self.assertIsNone(shaper.flow_warning)

    def test_failed_retirement_reports_incomplete_rollback(self):
        shaper, host = _shaper()
        host.files["/proc/42/stat"] = "42 (work) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        host.files.pop("/proc/42/stat")
        with patch.object(shaper, "_sync", side_effect=[ShaperError("retire failed"),
                                                       ShaperError("restore failed")]):
            shaper.sample()
        self.assertIn("retire failed", shaper.flow_warning)
        self.assertIn("rollback incomplete", shaper.flow_warning)
        self.assertIn("restore failed", shaper.flow_warning)
        self.assertIn(42, shaper.shares)

    def test_failed_remove_does_not_move_changed_or_unverified_pid_back(self):
        for new_stat in ("42 (other) S " + "0 " * 18 + "200", "malformed", None):
            with self.subTest(new_stat=new_stat):
                shaper, host = _shaper()
                path = "/proc/42/stat"
                host.files[path] = "42 (work) S " + "0 " * 18 + "100"
                shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)

                def fail_after_restore():
                    if new_stat is None:
                        host.files.pop(path)
                    else:
                        host.files[path] = new_stat
                    raise ShaperError("sync failed")

                with patch.object(shaper, "_sync", side_effect=fail_after_restore) as sync, \
                     patch.object(shaper, "_move_to_leaf", wraps=shaper._move_to_leaf) as move:
                    with self.assertRaisesRegex(ShaperError, "sync failed.*rollback incomplete.*cannot verify pid 42"):
                        shaper.remove(42)
                move.assert_not_called()
                self.assertEqual(sync.call_count, 1)
                self.assertIn(42, shaper.shares)
                if new_stat == "malformed":
                    _open_socket(host)
                    host.commands.clear()
                    shaper.sample()
                    self.assertIn("cannot verify pid 42", shaper.flow_warning)
                    self.assertFalse(any("flower" in cmd and "filter add" in cmd for cmd in _joined(host)))

    def test_sample_keeps_share_when_identity_is_unreadable(self):
        shaper, host = _shaper()
        stat = "/proc/42/stat"
        host.files[stat] = "42 (steam) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        host.files[stat] = "not a stat record"
        shaper.sample()
        self.assertIn(42, shaper.shares)
        with self.assertRaises(ShaperError):
            shaper.remove(42)
        self.assertIn(42, shaper.shares)

    def test_expected_start_time_is_checked_before_changing_anything(self):
        shaper, host = _shaper()
        stat_path = "/proc/42/stat"
        for stat in (None, "42 (curl) S " + "0 " * 18 + "99", "invalid"):
            if stat is None:
                host.files.pop(stat_path, None)
            else:
                host.files[stat_path] = stat
            before = dict(host.files)
            with self.subTest(stat=stat), self.assertRaises(ShaperError):
                shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
            self.assertEqual(host.files, before)
            self.assertEqual(host.commands, [])
            self.assertEqual(shaper.shares, {})
        host.files[stat_path] = "42 (curl) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        self.assertIn(42, shaper.shares)

    def test_moves_cgroup_and_sets_both_directions(self):
        shaper, host = _shaper()
        text = shaper.apply(42, 30, 10, 100_000_000, 100_000_000, "curl")
        self.assertIn("30%", text)
        self.assertIn("10%", text)
        self.assertEqual(
            host.files["/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs"],
            "42\n",
        )
        commands = _joined(host)
        self.assertIn("modprobe ifb", commands)
        self.assertTrue(any("root handle 1: htb default 999" in cmd and "dev eth0" in cmd for cmd in commands))
        self.assertTrue(any("dev ifb-bandctl" in cmd and "rate 30000kbit ceil 30000kbit" in cmd for cmd in commands))
        self.assertTrue(any("dev eth0" in cmd and "rate 10000kbit ceil 10000kbit" in cmd for cmd in commands))
        nft = [body for _args, body in host.commands if body]
        self.assertTrue(nft)
        self.assertIn('level 2 "bandctl-42"', nft[-1])
        state = host.files["/run/bandctl.json"]
        self.assertIn("fq_codel", state)
        self.assertIn("session-1.scope", state)

    def test_upload_only_skips_ifb(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 20, None, 100_000_000)
        commands = _joined(host)
        self.assertFalse(any(cmd.startswith("modprobe") for cmd in commands))
        self.assertFalse(shaper.download_active)
        self.assertTrue(shaper.upload_active)

    def test_download_only_does_not_replace_root(self):
        shaper, host = _shaper()
        shaper.apply(42, 20, None, 100_000_000, None)
        commands = _joined(host)
        self.assertFalse(any("dev eth0 root handle 1:" in cmd for cmd in commands))
        self.assertTrue(any("parent ffff:" in cmd and "matchall" in cmd for cmd in commands))
        self.assertFalse(any("protocol all" in cmd for cmd in commands))
        self.assertTrue(shaper.download_active)
        self.assertFalse(shaper.upload_active)

    def test_change_updates_rate_without_a_second_filter(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        shaper.apply(42, None, 40, None, 100_000_000)
        commands = _joined(host)
        filters = [cmd for cmd in commands if " filter add " in cmd and " fw " in cmd]
        self.assertEqual(len(filters), 2)  # ip and ipv6, once
        self.assertTrue(any("rate 40000kbit ceil 40000kbit" in cmd for cmd in commands))

    def test_over_commit_keeps_ceil(self):
        shaper, host = _shaper()
        cap = 100_000_000
        shaper.apply(42, None, 60, None, cap)
        shaper.apply(43, None, 60, None, cap)
        rates, ceils, _default = htb_plan(
            [percent_to_bps(60, cap), percent_to_bps(60, cap)],
            cap,
        )
        commands = _joined(host)
        for rate, ceil in zip(rates, ceils):
            needle = f"rate {rate}kbit ceil {ceil}kbit"
            self.assertTrue(any(needle in cmd for cmd in commands), needle)
        self.assertEqual(ceils, [60000, 60000])
        self.assertTrue(all(rate < ceil for rate, ceil in zip(rates, ceils)))

    def test_refuses_existing_htb_and_restores_cgroup(self):
        shaper, host = _shaper()
        host.qdisc = "qdisc htb 1: root refcnt 2 default 0\n"
        with self.assertRaises(ShaperError) as caught:
            shaper.apply(42, None, 10, None, 100_000_000)
        self.assertIn("already has an htb", str(caught.exception))
        self.assertEqual(shaper.shares, {})
        restored = "/sys/fs/cgroup/user.slice/user-1000.slice/session-1.scope/cgroup.procs"
        self.assertEqual(host.files[restored], "42\n")

    def test_nft_failure_rolls_back_qdisc_and_cgroup(self):
        shaper, host = _shaper()
        host.fail_substr = "nft -f"
        with self.assertRaises(ShaperError):
            shaper.apply(42, 10, 10, 100_000_000, 100_000_000)
        self.assertEqual(shaper.shares, {})
        commands = _joined(host)
        self.assertTrue(any("qdisc add dev eth0 root fq_codel" in cmd for cmd in commands))
        self.assertFalse(host.ifb_exists)
        restored = "/sys/fs/cgroup/user.slice/user-1000.slice/session-1.scope/cgroup.procs"
        self.assertEqual(host.files[restored], "42\n")

    def test_remove_last_share_restores_qdisc(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        message = shaper.remove(42)
        self.assertIn("removed", message)
        self.assertFalse(shaper.upload_active)
        self.assertTrue(any("qdisc add dev eth0 root fq_codel" in cmd for cmd in _joined(host)))
        self.assertIn("/run/bandctl.json", host.files)
        shaper.teardown()
        self.assertNotIn("/run/bandctl.json", host.files)

    def test_sample_bits_per_second(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 50, None, 100_000_000)
        host.class_show["eth0"] = "class htb 1:10 parent 1:1\n Sent 0 bytes 0 pkt\n"
        with patch("bandctl.shaper.time.monotonic", side_effect=[1.0, 2.0]):
            self.assertEqual(shaper.sample()[42][1], 0.0)
            host.class_show["eth0"] = "class htb 1:10 parent 1:1\n Sent 1000000 bytes 8 pkt\n"
            _down, up = shaper.sample()[42]
        self.assertEqual(up, 8_000_000)

    def test_class_counter_gap_uses_time_since_its_last_sample(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 50, None, 100_000_000)
        with patch.object(shaper, "_sync_flows"), \
             patch.object(shaper, "_class_bytes", side_effect=[{10: 100}, {}, {10: 300}]), \
             patch("bandctl.shaper.time.monotonic", side_effect=[1.0, 2.0, 3.0]):
            self.assertEqual(shaper.sample()[42][1], 0.0)
            self.assertEqual(shaper.sample()[42][1], 0.0)
            self.assertEqual(shaper.sample()[42][1], 800.0)

    def test_class_counter_reset_and_non_increasing_time(self):
        shaper, host = _shaper()
        self.assertEqual(shaper._delta_bps("up", 10, 100, 1.0), 0.0)
        self.assertEqual(shaper._delta_bps("up", 10, 200, 1.0), 0.0)
        self.assertEqual(shaper._delta_bps("up", 10, 200, 2.0), 800.0)
        self.assertEqual(shaper._delta_bps("up", 10, 50, 3.0), 0.0)
        self.assertEqual(shaper._delta_bps("up", 10, 60, 4.0), 80.0)

    def test_recreated_class_starts_with_a_fresh_counter_baseline(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, 100_000_000, 100_000_000)
        with patch("bandctl.shaper.time.monotonic", side_effect=[1.0, 2.0]):
            host.class_show["eth0"] = "class htb 1:10 parent 1:1\n Sent 100 bytes 1 pkt\n"
            self.assertEqual(shaper.sample()[42][1], 0.0)
            shaper.apply(42, 10, None, 100_000_000, 100_000_000)
            shaper.apply(42, 10, 10, 100_000_000, 100_000_000)
            host.class_show["eth0"] = "class htb 1:10 parent 1:1\n Sent 200 bytes 1 pkt\n"
            self.assertEqual(shaper.sample()[42][1], 0.0)

    def test_teardown_is_idempotent(self):
        shaper, host = _shaper()
        shaper.apply(42, 10, 10, 100_000_000, 100_000_000)
        shaper.teardown()
        shaper.teardown()
        self.assertFalse(host.ifb_exists)
        self.assertNotIn("bandctl-42", host.dirs)

    def test_recover_keeps_unusable_state_and_blocks_shaping(self):
        invalid_states = ("", "{broken", "[]", "{}", '{"iface": ""}', "[" * 2000 + "]" * 2000)
        for state in invalid_states:
            with self.subTest(state=state[:30]):
                shaper, host = _shaper()
                host.files["/run/bandctl.json"] = state
                host.files["/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs"] = "42\n"

                notes = shaper.recover()

                self.assertTrue(any("recovery state" in note for note in notes))
                self.assertIsNotNone(shaper.recovery_error)
                self.assertEqual(host.files["/run/bandctl.json"], state)
                self.assertNotIn("/sys/fs/cgroup/cgroup.procs", host.files)
                self.assertEqual(host.commands, [])
                with self.assertRaises(ShaperError):
                    shaper.apply(42, None, 10, None, 100_000_000)

    def test_recover_rejects_duplicate_fields_before_cleanup(self):
        states = (
            '{"iface":"eth0","pids":{},"nft_owned":false,"nft_owned":true}',
            '{"iface":"eth0","pids":{"42":"/one","42":"/two"}}',
        )
        for state in states:
            with self.subTest(state=state):
                shaper, host = _shaper()
                host.files["/run/bandctl.json"] = state
                host.files["/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs"] = "42\n"

                notes = shaper.recover()

                self.assertTrue(any("invalid recovery state" in note for note in notes))
                self.assertIsNotNone(shaper.recovery_error)
                self.assertEqual(host.files["/run/bandctl.json"], state)
                self.assertNotIn("/sys/fs/cgroup/cgroup.procs", host.files)
                self.assertEqual(host.commands, [])

    def test_recover_keeps_unreadable_state_for_retry(self):
        shaper, host = _shaper()
        state = '{"iface": "eth0", "pids": {}}'
        host.files["/run/bandctl.json"] = state
        read = host.read

        def fail_state(path):
            if path == "/run/bandctl.json":
                raise PermissionError("read denied")
            return read(path)

        with patch.object(host, "read", side_effect=fail_state):
            notes = shaper.recover()
        self.assertTrue(any("read denied" in note for note in notes))
        self.assertIsNotNone(shaper.recovery_error)
        self.assertEqual(host.files["/run/bandctl.json"], state)
        self.assertEqual(host.commands, [])

    def test_recovery_rejects_a_fifo_without_waiting_for_a_writer(self):
        import os
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.fifo"
            os.mkfifo(path)
            code = (
                "from bandctl.shaper import LinuxHost, Shaper, ShaperError\n"
                "import sys\n"
                "try:\n"
                "    Shaper('eth0', host=LinuxHost(), state_path=sys.argv[1])._read_state()\n"
                "except ShaperError as exc:\n"
                "    print(exc)\n"
            )
            try:
                result = subprocess.run([sys.executable, "-c", code, str(path)],
                                        capture_output=True, text=True, timeout=2, check=True)
            except subprocess.TimeoutExpired:
                self.fail("recovery waited for a FIFO writer")
            self.assertIn("regular file", result.stdout)
            self.assertTrue(path.exists())

    def test_recover_rejects_invalid_pid_mapping_and_keeps_state(self):
        shaper, host = _shaper()
        state = json.dumps({"iface": "eth0", "pids": [42]})
        host.files["/run/bandctl.json"] = state

        notes = shaper.recover()

        self.assertIn("invalid recovery state: pids must be an object", notes)
        self.assertIn("invalid recovery state", shaper.recovery_error)
        self.assertEqual(host.files["/run/bandctl.json"], state)
        self.assertEqual(host.commands, [])
        with self.assertRaises(ShaperError):
            shaper.apply(42, None, 10, None, 100_000_000)

    def test_recover_rejects_invalid_qdisc_before_cleanup(self):
        for qdisc in ("htb", "bad-qdisc", "", None, 5, ["fq"]):
            with self.subTest(qdisc=qdisc):
                shaper, host = _shaper()
                state = json.dumps({"iface": "eth0", "qdisc": qdisc, "pids": {},
                                    "root_replaced": True})
                host.files["/run/bandctl.json"] = state
                host.files["/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs"] = "42\n"

                notes = shaper.recover()

                self.assertTrue(any("invalid recovery state" in note for note in notes))
                self.assertIsNotNone(shaper.recovery_error)
                self.assertEqual(host.files["/run/bandctl.json"], state)
                self.assertNotIn("/sys/fs/cgroup/cgroup.procs", host.files)
                self.assertEqual(host.commands, [])

    def test_recover_rejects_invalid_interface_before_cleanup(self):
        for iface in ("..", "eth/0", "eth 0", "eth\x000", "a" * 16, "\ud800"):
            with self.subTest(iface=iface):
                shaper, host = _shaper()
                state = json.dumps({"iface": iface, "pids": {"42": "/original"}})
                host.files["/run/bandctl.json"] = state
                host.files["/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs"] = "42\n"

                notes = shaper.recover()

                self.assertTrue(any("invalid recovery state" in note for note in notes))
                self.assertIsNotNone(shaper.recovery_error)
                self.assertEqual(host.files["/run/bandctl.json"], state)
                self.assertNotIn("/sys/fs/cgroup/cgroup.procs", host.files)
                self.assertEqual(host.commands, [])

    def test_recover_rejects_invalid_pid_entries_before_cleanup(self):
        for pid, original in (("bad", "/original"), ("0", "/original"),
                              ("-1", "/original"), ("042", "/original"),
                              ("2147483648", "/original"), ("42", None),
                              ("42", "relative"), ("42", "/../outside"), ("42", "/bad\x00path"),
                              ("42", "/bad\ud800path")):
            with self.subTest(pid=pid, original=original):
                shaper, host = _shaper()
                state = json.dumps({"iface": "eth0", "pids": {"43": "/original", pid: original}})
                host.files["/run/bandctl.json"] = state
                host.files["/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs"] = "42\n"

                notes = shaper.recover()

                self.assertTrue(any("invalid recovery state" in note for note in notes))
                self.assertIsNotNone(shaper.recovery_error)
                self.assertEqual(host.files["/run/bandctl.json"], state)
                self.assertNotIn("/sys/fs/cgroup/cgroup.procs", host.files)
                self.assertEqual(host.commands, [])

    def test_recover_uses_state_file(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        # A new process, same fake machine, as after a crash.
        again, _same = _shaper(host)
        host.files["/proc/42/cgroup"] = "0::/bandctl/bandctl-42\n"
        notes = again.recover()
        self.assertTrue(any("restored pid 42" in note for note in notes))
        self.assertTrue(any("qdisc add dev eth0 root fq_codel" in cmd for cmd in _joined(host)))
        self.assertNotIn("/run/bandctl.json", host.files)

    def test_recover_does_not_restore_a_reused_pid_outside_the_leaf(self):
        shaper, host = _shaper()
        host.files["/proc/42/stat"] = "42 (curl) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        host.files["/proc/42/stat"] = "42 (other) S " + "0 " * 18 + "200"
        host.files["/proc/42/cgroup"] = "0::/other.slice\n"
        host.files["/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs"] = ""
        again = Shaper(iface="eth0", host=host, state_path="/run/bandctl.json")
        notes = again.recover()
        original = "/sys/fs/cgroup/user.slice/user-1000.slice/session-1.scope/cgroup.procs"
        self.assertNotIn(original, host.files)
        self.assertFalse(any(note == "restored pid 42" for note in notes))

    def test_recover_rejects_invalid_ownership_flags_before_cleanup(self):
        for field in ("root_replaced", "ifb_owned", "ingress_owned", "nft_owned"):
            for value in ("false", None, 0):
                with self.subTest(field=field, value=value):
                    shaper, host = _shaper()
                    state = json.dumps({"iface": "eth0", "pids": {}, field: value})
                    host.files["/run/bandctl.json"] = state
                    host.ifb_exists = True

                    notes = shaper.recover()

                    self.assertTrue(any(field in note for note in notes))
                    self.assertIsNotNone(shaper.recovery_error)
                    self.assertEqual(host.files["/run/bandctl.json"], state)
                    self.assertTrue(host.ifb_exists)
                    self.assertEqual(host.commands, [])

    def test_recover_rejects_incomplete_download_ownership(self):
        for field in ("ifb_owned", "ingress_owned"):
            with self.subTest(field=field):
                shaper, host = _shaper()
                state = json.dumps({"iface": "eth0", "pids": {}, field: False})
                host.files["/run/bandctl.json"] = state
                host.ifb_exists = True

                notes = shaper.recover()

                self.assertTrue(any("ownership" in note for note in notes))
                self.assertIsNotNone(shaper.recovery_error)
                self.assertEqual(host.files["/run/bandctl.json"], state)
                self.assertTrue(host.ifb_exists)
                self.assertEqual(host.commands, [])

    def test_recover_rejects_incomplete_or_invalid_start_times(self):
        for starts in ([], {}, {"42": "100"}, {"42": True}):
            with self.subTest(start_ticks=starts):
                shaper, host = _shaper()
                state = json.dumps({"iface": "eth0", "pids": {"42": "/original"},
                                    "start_ticks": starts})
                host.files["/run/bandctl.json"] = state
                host.files["/proc/42/cgroup"] = "0::/bandctl/bandctl-42\n"
                host.files["/proc/42/stat"] = "42 (other) S " + "0 " * 18 + "200"
                host.files["/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs"] = "42\n"

                notes = shaper.recover()

                self.assertTrue(any("start_ticks" in note for note in notes))
                self.assertIsNotNone(shaper.recovery_error)
                self.assertEqual(host.files["/run/bandctl.json"], state)
                self.assertNotIn("/sys/fs/cgroup/original/cgroup.procs", host.files)
                self.assertNotIn("/sys/fs/cgroup/cgroup.procs", host.files)
                self.assertEqual(host.commands, [])

    def test_recover_checks_start_time_even_if_reused_pid_is_in_leaf(self):
        shaper, host = _shaper()
        host.files["/proc/42/stat"] = "42 (curl) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        host.files["/proc/42/stat"] = "42 (other) S " + "0 " * 18 + "200"
        host.files["/proc/42/cgroup"] = "0::/bandctl/bandctl-42\n"
        host.files["/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs"] = "42\n"
        again = Shaper(iface="eth0", host=host, state_path="/run/bandctl.json")
        again.recover()
        original = "/sys/fs/cgroup/user.slice/user-1000.slice/session-1.scope/cgroup.procs"
        self.assertNotIn(original, host.files)
        self.assertNotIn("/sys/fs/cgroup/cgroup.procs", host.files)
        self.assertIn("cannot verify pid 42", again.recovery_error)
        self.assertIn("/run/bandctl.json", host.files)
        host.files["/proc/42/cgroup"] = "0::/other.slice\n"
        host.files["/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs"] = ""
        retry = Shaper(iface="eth0", host=host, state_path="/run/bandctl.json")
        retry.recover()
        self.assertIsNone(retry.recovery_error)
        self.assertNotIn("/run/bandctl.json", host.files)

    def test_recover_restores_verified_pid_in_its_leaf(self):
        shaper, host = _shaper()
        host.files["/proc/42/stat"] = "42 (curl) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        host.files["/proc/42/cgroup"] = "0::/bandctl/bandctl-42\n"
        again = Shaper(iface="eth0", host=host, state_path="/run/bandctl.json")
        self.assertIn("restored pid 42", again.recover())
        original = "/sys/fs/cgroup/user.slice/user-1000.slice/session-1.scope/cgroup.procs"
        self.assertEqual(host.files[original], "42\n")

    def test_recover_old_state_checks_cgroup_before_restoring(self):
        shaper, host = _shaper()
        original = "/user.slice/user-1000.slice/session-1.scope"
        host.files["/run/bandctl.json"] = json.dumps({"iface": "eth0", "pids": {"42": original}})
        host.files["/proc/42/cgroup"] = "0::/other.slice\n"
        shaper.recover()
        self.assertNotIn("/sys/fs/cgroup" + original + "/cgroup.procs", host.files)

    def test_recover_does_not_restore_when_start_time_cannot_be_read(self):
        shaper, host = _shaper()
        host.files["/proc/42/stat"] = "42 (curl) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        host.files["/proc/42/cgroup"] = "0::/bandctl/bandctl-42\n"
        del host.files["/proc/42/stat"]
        again = Shaper(iface="eth0", host=host, state_path="/run/bandctl.json")
        self.assertIn("cannot verify pid 42", again.recover())
        original = "/sys/fs/cgroup/user.slice/user-1000.slice/session-1.scope/cgroup.procs"
        self.assertNotIn(original, host.files)
        self.assertIn("cannot verify pid 42", again.recovery_error)
        self.assertIn("/run/bandctl.json", host.files)
        self.assertNotIn("/sys/fs/cgroup/cgroup.procs", host.files)

    def test_recover_retries_when_process_cgroup_cannot_be_read(self):
        shaper, host = _shaper()
        host.files["/proc/42/stat"] = "42 (curl) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        host.files["/proc/42/cgroup"] = "0::/bandctl/bandctl-42\n"
        host.files["/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs"] = "42\n"
        original = "/sys/fs/cgroup/user.slice/user-1000.slice/session-1.scope/cgroup.procs"
        read = host.read

        def fail_cgroup(path):
            if path == "/proc/42/cgroup":
                raise PermissionError("read denied")
            return read(path)

        with patch.object(host, "read", side_effect=fail_cgroup):
            again = Shaper(iface="eth0", host=host, state_path="/run/bandctl.json")
            again.recover()
            self.assertIn("42", again.recovery_error)
            self.assertIn("/run/bandctl.json", host.files)
            self.assertNotIn("/sys/fs/cgroup/cgroup.procs", host.files)

        retry = Shaper(iface="eth0", host=host, state_path="/run/bandctl.json")
        retry.recover()
        self.assertIsNone(retry.recovery_error)
        self.assertEqual(host.files[original], "42\n")
        self.assertNotIn("/run/bandctl.json", host.files)

    def test_recover_cleans_the_saved_interface_not_the_new_one(self):
        shaper, host = _shaper()
        shaper.apply(42, 10, 10, 100_000_000, 100_000_000)
        host.commands.clear()
        again = Shaper(iface="wlan0", host=host, state_path="/run/bandctl.json")
        again.recover()
        commands = _joined(host)
        self.assertIn("tc qdisc del dev eth0 ingress", commands)
        self.assertIn("tc qdisc del dev eth0 root", commands)
        self.assertIn("tc qdisc add dev eth0 root fq_codel", commands)
        self.assertFalse(any("dev wlan0" in command for command in commands))

    def test_recover_download_only_leaves_root_qdisc_alone(self):
        shaper, host = _shaper()
        shaper.apply(42, 10, None, 100_000_000, None)
        host.commands.clear()
        again = Shaper(iface="wlan0", host=host, state_path="/run/bandctl.json")
        again.recover()
        commands = _joined(host)
        self.assertIn("tc qdisc del dev eth0 ingress", commands)
        self.assertFalse(any("dev eth0 root" in command for command in commands))
        self.assertFalse(any("dev wlan0" in command for command in commands))

    def test_records_root_replacement_before_changing_qdisc(self):
        shaper, host = _shaper()
        shaper.apply(42, 10, None, 100_000_000, None)
        original_run = host.run
        recorded = []

        def inspect_state(args, input_text=None):
            if args[:4] == ["tc", "qdisc", "replace", "dev"] and args[4] == "eth0":
                recorded.append(json.loads(host.files["/run/bandctl.json"]))
            return original_run(args, input_text)

        host.run = inspect_state
        shaper.apply(42, 10, 10, 100_000_000, 100_000_000)
        self.assertEqual(len(recorded), 1)
        self.assertTrue(recorded[0]["root_replaced"])
        self.assertEqual(recorded[0]["qdisc"], "fq_codel")

    def test_recover_older_state_restores_saved_root_qdisc(self):
        shaper, host = _shaper()
        host.files["/run/bandctl.json"] = json.dumps({"iface": "eth0", "qdisc": "fq", "pids": {}})
        shaper.recover()
        self.assertIn("tc qdisc add dev eth0 root fq", _joined(host))

    def test_restore_reports_a_cgroup_write_error(self):
        shaper, host = _shaper()

        def boom(path: str, data: str) -> None:
            raise ShaperError(f"{path}: Device or resource busy")

        host.write = boom  # type: ignore[method-assign]
        with self.assertRaises(ShaperError) as caught:
            shaper._restore_pid(42, "/user.slice/session-1.scope")
        self.assertIn("could not restore pid 42", str(caught.exception))

    @patch("sys.byteorder", "little")
    def test_apply_shapes_sockets_already_open(self):
        shaper, host = _shaper()
        _open_socket(host)
        shaper.apply(42, 30, 10, 100_000_000, 100_000_000)
        commands = _joined(host)
        upload = [cmd for cmd in commands if "flower" in cmd and "classid 1:10" in cmd]
        download = [cmd for cmd in commands if "flower" in cmd and "skbedit mark 10" in cmd]
        self.assertEqual(upload, [
            "tc filter add dev eth0 parent 1: protocol ip prio 2 handle 1 "
            "flower ip_proto tcp src_ip 10.0.0.8 src_port 40000 "
            "dst_ip 1.2.3.4 dst_port 443 classid 1:10"
        ])
        self.assertEqual(download, [
            "tc filter add dev eth0 parent ffff: protocol ip prio 1 handle 2 "
            "flower ip_proto tcp dst_ip 10.0.0.8 dst_port 40000 "
            "src_ip 1.2.3.4 src_port 443 "
            "action skbedit mark 10 pipe action mirred egress redirect dev ifb-bandctl"
        ])
        self.assertFalse(any("127.0.0.1" in cmd for cmd in commands))
        added = sum(1 for cmd in commands if "filter add" in cmd and "flower" in cmd)
        shaper.sample()
        self.assertEqual(
            sum(1 for cmd in _joined(host) if "filter add" in cmd and "flower" in cmd),
            added,
        )

    def test_apply_uses_wildcard_option_for_open_udp_sockets(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                shaper, host = _shaper()
                shaper.match_wildcard = enabled
                host.files["/proc/net/udp"] = (
                    "sl local_address rem_address st tx_queue rx_queue tr tm retrnsmt uid timeout inode\n"
                    "0: 00000000:14E9 00000000:0000 07 0 0 0 0 0 99\n"
                )
                host.links["/proc/42/fd/3"] = "socket:[99]"
                shaper.apply(42, 30, 10, 100_000_000, 100_000_000)
                filters = [args for args, _ in host.commands if "flower" in args and "add" in args]
                self.assertEqual(len(filters), 2)
                for args in filters:
                    self.assertEqual("0.0.0.0" in args, not enabled)
                    self.assertIn("5353", args)

    def test_wildcard_flows_with_one_local_port_share_one_filter(self):
        shaper, host = _shaper()
        shaper.match_wildcard = True
        host.files["/proc/net/udp"] = (
            "sl local_address rem_address st tx_queue rx_queue tr tm retrnsmt uid timeout inode\n"
            "0: 00000000:14E9 04030201:01BB 01 0 0 0 0 0 99\n"
            "1: 00000000:14E9 08080808:0035 01 0 0 0 0 0 100\n"
        )
        host.links["/proc/42/fd/3"] = "socket:[99]"
        host.links["/proc/42/fd/4"] = "socket:[100]"
        shaper.apply(42, None, 10, None, 100_000_000)
        filters = [args for args, _ in host.commands if "flower" in args and "add" in args]
        self.assertEqual(len(filters), 1)
        self.assertIn("5353", filters[0])
        shaper.sample()
        self.assertEqual(len([args for args, _ in host.commands if "flower" in args and "add" in args]), 1)

    @patch("sys.byteorder", "little")
    def test_specific_udp_filters_take_precedence_over_wildcard_filters(self):
        for order in ((42, 43), (43, 42)):
            with self.subTest(order=order):
                shaper, host = _shaper()
                shaper.match_wildcard = True
                host.files["/proc/net/udp"] = (
                    "sl local_address rem_address st tx_queue rx_queue tr tm retrnsmt uid timeout inode\n"
                    "0: 00000000:14E9 00000000:0000 07 0 0 0 0 0 99\n"
                    "1: 0800000A:14E9 00000000:0000 07 0 0 0 0 0 100\n"
                )
                host.links["/proc/42/fd/3"] = "socket:[99]"
                host.links["/proc/43/fd/3"] = "socket:[100]"
                for pid in order:
                    shaper.apply(pid, 30, 10, 100_000_000, 100_000_000)
                filters = [args for args, _ in host.commands if "flower" in args and "add" in args]
                for parent in ("1:", "ffff:"):
                    direction = [args for args in filters if args[args.index("parent") + 1] == parent]
                    specific = next(args for args in direction if "10.0.0.8" in args)
                    wildcard = next(args for args in direction if "src_ip" not in args and "dst_ip" not in args)
                    self.assertLess(int(specific[specific.index("prio") + 1]),
                                    int(wildcard[wildcard.index("prio") + 1]))
                shaper.remove(42)
                deleted = [args for args, _ in host.commands if "flower" in args and "del" in args]
                self.assertEqual({(args[args.index("parent") + 1], args[args.index("prio") + 1])
                                  for args in deleted}, {("1:", "3"), ("ffff:", "2")})

    def test_upload_flower_does_not_touch_ingress(self):
        shaper, host = _shaper()
        _open_socket(host)
        shaper.apply(42, None, 20, None, 100_000_000)
        commands = _joined(host)
        self.assertTrue(any("flower" in cmd and "classid 1:10" in cmd for cmd in commands))
        self.assertFalse(any("parent ffff:" in cmd for cmd in commands))
        self.assertFalse(any(cmd.startswith("modprobe ifb") for cmd in commands))
        self.assertTrue(any("modprobe cls_flower" in cmd for cmd in commands))

    @patch("sys.byteorder", "little")
    def test_sample_adds_and_drops_flower_filters(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        _open_socket(host)
        shaper.sample()
        self.assertTrue(any("src_ip 10.0.0.8" in cmd and "flower" in cmd for cmd in _joined(host)))
        host.links.clear()
        shaper.sample()
        deleted = [cmd for cmd in _joined(host) if "filter del" in cmd and "flower" in cmd]
        self.assertEqual(len(deleted), 1)
        self.assertEqual(shaper._flows, {})

    def test_reused_pid_during_socket_scan_keeps_existing_filters(self):
        shaper, host = _shaper()
        stat = "/proc/42/stat"
        host.files[stat] = "42 (old) S " + "0 " * 18 + "100"
        _open_socket(host)
        shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        existing = dict(shaper._flows)
        self.assertTrue(existing)
        host.commands.clear()

        def scan_replacement(pid, net):
            host.files[stat] = "42 (new) S " + "0 " * 18 + "200"
            return [SockFlow("tcp", "ip", "10.0.0.8", 40001, "1.2.3.4", 443)]

        with patch.object(shaper, "_pid_flows", side_effect=scan_replacement):
            shaper.sample()
        self.assertIn("cannot verify pid 42", shaper.flow_warning)
        self.assertEqual(shaper._flows, existing)
        self.assertFalse(any("flower" in command for command in _joined(host)))

    def test_failed_socket_scan_keeps_existing_filters_until_retry(self):
        for operation, path, error in (
            ("read", "/proc/net/tcp", PermissionError("read denied")),
            ("read", "/proc/net/tcp", FileNotFoundError("table disappeared")),
            ("listdir", "/proc/42/fd", PermissionError("read denied")),
            ("readlink", "/proc/42/fd/3", PermissionError("read denied")),
        ):
            with self.subTest(operation=operation, error=type(error).__name__):
                shaper, host = _shaper()
                _open_socket(host)
                shaper.apply(42, None, 10, None, 100_000_000)
                existing = dict(shaper._flows)
                self.assertTrue(existing)
                original = getattr(host, operation)

                def fail_target(target):
                    if target == path:
                        raise error
                    return original(target)

                host.commands.clear()
                with patch.object(host, operation, side_effect=fail_target):
                    shaper.sample()
                self.assertIsNotNone(shaper.flow_warning)
                self.assertEqual(shaper._flows, existing)
                self.assertFalse(any("filter del" in command and "flower" in command
                                     for command in _joined(host)))
                host.links.clear()
                shaper.sample()
                self.assertIsNone(shaper.flow_warning)
                self.assertEqual(shaper._flows, {})

    def test_malformed_socket_table_keeps_filters_until_retry(self):
        header = "sl local_address rem_address inode\n"
        for damaged in ("", "not a socket table\n", header + "0: truncated\n",
                        header + "0: invalid:0050 04030201:01BB 01 0 0 0 0 0 99\n",
                        header + "0: 0800000A:9C4 04030201:01BB 01 0 0 0 0 0 99\n",
                        header + "0: 0800000A:9C40 04030201:+1BB 01 0 0 0 0 0 99\n"):
            with self.subTest(damaged=damaged):
                shaper, host = _shaper()
                _open_socket(host)
                shaper.apply(42, None, 10, None, 100_000_000)
                existing = dict(shaper._flows)
                self.assertTrue(existing)
                original = host.files["/proc/net/tcp"]
                host.files["/proc/net/tcp"] = damaged
                host.commands.clear()
                shaper.sample()
                self.assertIsNotNone(shaper.flow_warning)
                self.assertIn("/proc/net/tcp", shaper.flow_warning)
                self.assertEqual(shaper._flows, existing)
                self.assertFalse(any("filter del" in command and "flower" in command
                                     for command in _joined(host)))
                host.files["/proc/net/tcp"] = original
                host.links.clear()
                shaper.sample()
                self.assertIsNone(shaper.flow_warning)
                self.assertEqual(shaper._flows, {})

    def test_first_scan_rejects_missing_ipv4_socket_tables(self):
        for missing in ("tcp", "udp"):
            with self.subTest(missing=missing):
                shaper, host = _shaper()
                _open_socket(host)
                host.files["/proc/net/udp"] = "sl local_address rem_address inode\n"
                del host.files[f"/proc/net/{missing}"]
                with self.assertRaisesRegex(ShaperError, f"cannot read /proc/net/{missing}"):
                    shaper.apply(42, None, 10, None, 100_000_000)
                self.assertNotIn(42, shaper.shares)

    def test_ipv6_socket_tables_may_be_absent_but_not_disappear(self):
        for filename in ("tcp6", "udp6"):
            with self.subTest(filename=filename):
                shaper, host = _shaper()
                _open_socket(host)
                host.files[f"/proc/net/{filename}"] = "sl local_address remote_address inode\n"
                shaper.apply(42, None, 10, None, 100_000_000)
                existing = dict(shaper._flows)
                self.assertTrue(existing)
                del host.files[f"/proc/net/{filename}"]
                shaper.sample()
                self.assertIn(f"/proc/net/{filename}", shaper.flow_warning)
                self.assertEqual(shaper._flows, existing)

    def test_sample_retries_failed_flower_deletion(self):
        shaper, host = _shaper()
        _open_socket(host)
        shaper.apply(42, None, 10, None, 100_000_000)
        host.links.clear()
        host.fail_substr = "tc filter del dev eth0 parent 1: protocol ip prio 2"

        shaper.sample()

        self.assertIn("forced failure", shaper.flow_warning)
        self.assertEqual(len(shaper._flows), 1)
        host.fail_substr = None
        shaper.sample()
        self.assertIsNone(shaper.flow_warning)
        self.assertEqual(shaper._flows, {})
        deletes = [cmd for cmd in _joined(host) if "filter del" in cmd and "flower" in cmd]
        self.assertEqual(len(deletes), 2)

    def test_sample_accepts_a_flower_filter_already_removed(self):
        shaper, host = _shaper()
        _open_socket(host)
        shaper.apply(42, None, 10, None, 100_000_000)
        host.links.clear()
        run = host.run

        def missing_filter(args, input_text=None):
            if args[:3] == ["tc", "filter", "del"] and args[-1] == "flower":
                raise ShaperError("RTNETLINK answers: No such file or directory", args)
            return run(args, input_text)

        with patch.object(host, "run", side_effect=missing_filter):
            shaper.sample()
        self.assertIsNone(shaper.flow_warning)
        self.assertEqual(shaper._flows, {})

    def test_removing_one_pid_drops_only_its_flower(self):
        shaper, host = _shaper()
        host.files["/proc/net/tcp"] = (
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            "   0: 0800000A:9C40 04030201:01BB 01 00000000:00000000 00:00000000 00000000     0        0 99 1 0000000000000000 100 0 0 10 0\n"
            "   1: 0900000A:9C41 04030201:01BB 01 00000000:00000000 00:00000000 00000000     0        0 100 1 0000000000000000 100 0 0 10 0\n"
        )
        host.links["/proc/42/fd/3"] = "socket:[99]"
        host.links["/proc/43/fd/3"] = "socket:[100]"
        shaper.apply(42, None, 10, None, 100_000_000)
        shaper.apply(43, None, 10, None, 100_000_000)
        self.assertEqual(len(shaper._flows), 2)
        shaper.remove(42)
        deleted = [cmd for cmd in _joined(host) if "filter del" in cmd and "flower" in cmd]
        self.assertEqual(deleted, [
            "tc filter del dev eth0 parent 1: protocol ip prio 2 handle 1 flower"
        ])
        self.assertEqual(len(shaper._flows), 1)


class CleanupTests(unittest.TestCase):
    """Fixes for teardown, remove, and failed apply."""

    def test_failed_state_removal_is_reported_and_recovery_blocks_startup(self):
        for operation in ("recover", "teardown"):
            with self.subTest(operation=operation):
                shaper, host = _shaper()
                host.files["/run/bandctl.json"] = json.dumps({
                    "iface": "eth0", "pids": {}, "root_replaced": False,
                    "ifb_owned": False, "ingress_owned": False, "nft_owned": False,
                })
                with patch.object(host, "unlink", side_effect=PermissionError("unlink denied")):
                    notes = getattr(shaper, operation)()
                self.assertTrue(any("unlink denied" in note for note in notes))
                self.assertIn("/run/bandctl.json", host.files)
                if operation == "recover":
                    self.assertIn("unlink denied", shaper.recovery_error)
                    with self.assertRaisesRegex(ShaperError, "shut down"):
                        shaper.apply(42, None, 10, None, 100_000_000)
                retry = Shaper(iface="eth0", host=host, state_path="/run/bandctl.json")
                retry.recover()
                self.assertIsNone(retry.recovery_error)
                self.assertNotIn("/run/bandctl.json", host.files)

    def test_missing_cleanup_commands_keep_recovery_state(self):
        for missing in ("nft", "tc", "ip"):
            with self.subTest(command=missing):
                shaper, host = _shaper()
                shaper.apply(42, 10, None, 100_000_000, None)
                run = host.run

                def missing_command(args, input_text=None):
                    if args[0] == missing:
                        raise ShaperError(f"command not found: {missing}", args)
                    return run(args, input_text)

                with patch.object(host, "run", side_effect=missing_command):
                    notes = shaper.teardown()
                self.assertTrue(any(f"command not found: {missing}" in note for note in notes))
                self.assertIn("/run/bandctl.json", host.files)
                if missing == "tc":
                    self.assertTrue(host.ifb_exists)

    def test_idle_session_does_not_delete_existing_nft_table(self):
        shaper, host = _shaper()
        shaper.recover()
        shaper.teardown()
        self.assertNotIn("nft delete table inet bandctl", _joined(host))

    def test_apply_refuses_existing_nft_table_without_flushing_or_deleting_it(self):
        shaper, host = _shaper()
        original_run = host.run

        def existing_table(args, input_text=None):
            if args == ["nft", "create", "table", "inet", "bandctl"]:
                host.commands.append((list(args), input_text))
                raise ShaperError("File exists", args)
            return original_run(args, input_text)

        host.run = existing_table
        with self.assertRaisesRegex(ShaperError, "File exists"):
            shaper.apply(42, None, 10, None, 100_000_000)
        commands = _joined(host)
        self.assertNotIn("nft -f -", commands)
        self.assertNotIn("nft delete table inet bandctl", commands)
        self.assertFalse(any("qdisc replace dev eth0 root" in cmd for cmd in commands))

    def test_recovery_with_unowned_nft_table_leaves_it_alone(self):
        shaper, host = _shaper()
        host.files["/run/bandctl.json"] = json.dumps({
            "iface": "eth0", "root_replaced": False, "ifb_owned": False,
            "ingress_owned": False, "nft_owned": False, "pids": {},
        })
        shaper.recover()
        self.assertNotIn("nft delete table inet bandctl", _joined(host))

    def test_nft_ownership_is_recorded_before_populating_table(self):
        shaper, host = _shaper()
        original_run = host.run
        recorded = []

        def inspect_state(args, input_text=None):
            if args == ["nft", "-f", "-"]:
                recorded.append(json.loads(host.files["/run/bandctl.json"]))
            return original_run(args, input_text)

        host.run = inspect_state
        shaper.apply(42, None, 10, None, 100_000_000)
        shaper.apply(42, None, 20, None, 100_000_000)
        self.assertEqual(len(recorded), 2)
        self.assertTrue(all(state["nft_owned"] for state in recorded))
        self.assertEqual(_joined(host).count("nft create table inet bandctl"), 1)
        shaper.remove(42)
        self.assertEqual(_joined(host).count("nft delete table inet bandctl"), 1)

    def test_recovery_of_old_state_cleans_nft_table(self):
        shaper, host = _shaper()
        host.files["/run/bandctl.json"] = json.dumps({"iface": "eth0", "pids": {}})
        shaper.recover()
        self.assertIn("nft delete table inet bandctl", _joined(host))

    def test_failed_nft_cleanup_keeps_state_and_blocks_recovery_until_retry(self):
        shaper, host = _shaper()
        shaper.apply(42, 10, None, 100_000_000, None)
        host.fail_substr = "nft delete table"
        notes = shaper.teardown()
        self.assertTrue(any("forced failure" in note for note in notes))
        self.assertIn("/run/bandctl.json", host.files)
        again = Shaper("eth0", host=host, state_path="/run/bandctl.json")
        again.recover()
        self.assertIn("forced failure", again.recovery_error)
        self.assertIn("/run/bandctl.json", host.files)
        host.fail_substr = None
        retry = Shaper("eth0", host=host, state_path="/run/bandctl.json")
        retry.recover()
        self.assertIsNone(retry.recovery_error)
        self.assertNotIn("/run/bandctl.json", host.files)

    def test_nft_cleanup_failure_does_not_repeat_completed_root_restore(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        host.fail_substr = "nft delete table"
        shaper.teardown()
        state = json.loads(host.files["/run/bandctl.json"])
        self.assertFalse(state["root_replaced"])
        self.assertEqual(_joined(host).count("tc qdisc add dev eth0 root fq_codel"), 1)
        host.commands.clear()
        host.fail_substr = None
        Shaper("eth0", host=host, state_path="/run/bandctl.json").recover()
        self.assertNotIn("tc qdisc del dev eth0 root", _joined(host))

    def test_failed_ingress_removal_keeps_ifb_and_share_for_retry(self):
        shaper, host = _shaper()
        shaper.apply(42, 10, None, 100_000_000, None)
        host.fail_substr = "tc qdisc del dev eth0 ingress"
        with self.assertRaisesRegex(ShaperError, "forced failure"):
            shaper.remove(42)
        self.assertIn(42, shaper.shares)
        self.assertTrue(host.ifb_exists)
        self.assertNotIn("ip link del ifb-bandctl", _joined(host))
        host.fail_substr = None
        shaper.remove(42)
        self.assertFalse(host.ifb_exists)

    def test_failed_ifb_removal_preserves_state_for_recovery(self):
        shaper, host = _shaper()
        shaper.apply(42, 10, None, 100_000_000, None)
        host.fail_substr = "ip link del ifb-bandctl"
        notes = shaper.teardown()
        self.assertTrue(any("forced failure" in note for note in notes))
        self.assertTrue(host.ifb_exists)
        state = json.loads(host.files["/run/bandctl.json"])
        self.assertTrue(state["ifb_owned"])
        self.assertFalse(state["ingress_owned"])
        again = Shaper("eth0", host=host, state_path="/run/bandctl.json")
        again.recover()
        self.assertIn("forced failure", again.recovery_error)
        self.assertIn("/run/bandctl.json", host.files)
        host.fail_substr = None
        retry = Shaper("eth0", host=host, state_path="/run/bandctl.json")
        retry.recover()
        self.assertFalse(host.ifb_exists)
        self.assertNotIn("/run/bandctl.json", host.files)

    def test_retry_after_failed_ifb_removal_does_not_delete_ingress_twice(self):
        shaper, host = _shaper()
        shaper.apply(42, 10, None, 100_000_000, None)
        host.fail_substr = "ip link del ifb-bandctl"
        with self.assertRaises(ShaperError):
            shaper.remove(42)
        host.fail_substr = None
        shaper.remove(42)
        self.assertEqual(_joined(host).count("tc qdisc del dev eth0 ingress"), 1)

    def test_recovery_accepts_already_removed_download_objects(self):
        shaper, host = _shaper()
        host.files["/run/bandctl.json"] = json.dumps({
            "iface": "eth0", "root_replaced": False, "ifb_owned": True,
            "ingress_owned": True, "pids": {},
        })
        original_run = host.run

        def missing(args, input_text=None):
            if args == ["tc", "qdisc", "del", "dev", "eth0", "ingress"]:
                raise ShaperError("No such file or directory", args)
            if args == ["ip", "link", "del", "ifb-bandctl"]:
                raise ShaperError('Cannot find device "ifb-bandctl"', args)
            return original_run(args, input_text)

        host.run = missing
        shaper.recover()
        self.assertIsNone(shaper.recovery_error)
        self.assertNotIn("/run/bandctl.json", host.files)

    def test_recovery_without_state_leaves_existing_ifb_alone(self):
        shaper, host = _shaper()
        host.ifb_exists = True
        shaper.recover()
        self.assertTrue(host.ifb_exists)
        self.assertNotIn("ip link del ifb-bandctl", _joined(host))

    def test_recovery_of_upload_only_session_leaves_existing_ifb_alone(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        host.ifb_exists = True
        host.commands.clear()
        again = Shaper("eth0", host=host, state_path="/run/bandctl.json")
        again.recover()
        self.assertTrue(host.ifb_exists)
        self.assertNotIn("ip link del ifb-bandctl", _joined(host))
        self.assertNotIn("tc qdisc del dev eth0 ingress", _joined(host))

    def test_recovery_of_partial_download_setup_leaves_foreign_ingress_alone(self):
        shaper, host = _shaper()
        host.ifb_exists = True
        host.files["/run/bandctl.json"] = json.dumps({
            "iface": "eth0", "root_replaced": False, "ifb_owned": True,
            "ingress_owned": False, "pids": {},
        })
        shaper.recover()
        self.assertFalse(host.ifb_exists)
        self.assertNotIn("tc qdisc del dev eth0 ingress", _joined(host))

    def test_recovery_of_old_state_cleans_existing_ifb(self):
        shaper, host = _shaper()
        host.ifb_exists = True
        host.files["/run/bandctl.json"] = json.dumps({"iface": "eth0", "pids": {}})
        shaper.recover()
        self.assertFalse(host.ifb_exists)
        self.assertIn("tc qdisc del dev eth0 ingress", _joined(host))

    def test_legacy_recovery_keeps_state_when_ifb_probe_fails(self):
        shaper, host = _shaper()
        state = json.dumps({"iface": "eth0", "pids": {}, "root_replaced": False, "nft_owned": False})
        host.files["/run/bandctl.json"] = state
        host.ifb_exists = True
        run = host.run

        def missing_ip(args, input_text=None):
            if args[:3] == ["ip", "link", "show"]:
                raise ShaperError("command not found: ip", args)
            return run(args, input_text)

        with patch.object(host, "run", side_effect=missing_ip):
            notes = shaper.recover()
        self.assertTrue(any("command not found: ip" in note for note in notes))
        self.assertIn("command not found: ip", shaper.recovery_error)
        self.assertEqual(host.files["/run/bandctl.json"], state)
        self.assertTrue(host.ifb_exists)
        retry = Shaper(iface="eth0", host=host, state_path="/run/bandctl.json")
        retry.recover()
        self.assertIsNone(retry.recovery_error)
        self.assertFalse(host.ifb_exists)
        self.assertNotIn("/run/bandctl.json", host.files)

    def test_download_records_ifb_before_adding_ingress(self):
        shaper, host = _shaper()
        original_run = host.run
        recorded = []

        def inspect_state(args, input_text=None):
            if args == ["tc", "qdisc", "add", "dev", "eth0", "handle", "ffff:", "ingress"]:
                recorded.append(json.loads(host.files["/run/bandctl.json"]))
            return original_run(args, input_text)

        host.run = inspect_state
        shaper.apply(42, 10, None, 100_000_000, None)
        self.assertTrue(recorded[0]["ifb_owned"])
        self.assertFalse(recorded[0]["ingress_owned"])
        state = json.loads(host.files["/run/bandctl.json"])
        self.assertTrue(state["ingress_owned"])

    def test_download_refuses_existing_ifb_without_deleting_it(self):
        shaper, host = _shaper()
        host.ifb_exists = True
        with self.assertRaises(ShaperError):
            shaper.apply(42, 10, None, 100_000_000, None)
        self.assertTrue(host.ifb_exists)
        self.assertNotIn("ip link del ifb-bandctl", _joined(host))
        self.assertNotIn("tc qdisc del dev eth0 ingress", _joined(host))

    def test_download_refuses_existing_ingress_without_deleting_it(self):
        shaper, host = _shaper()
        original_run = host.run

        def existing_ingress(args, input_text=None):
            if args == ["tc", "qdisc", "add", "dev", "eth0", "handle", "ffff:", "ingress"]:
                host.commands.append((list(args), input_text))
                raise ShaperError("RTNETLINK answers: File exists", args)
            return original_run(args, input_text)

        host.run = existing_ingress
        with self.assertRaises(ShaperError):
            shaper.apply(42, 10, None, 100_000_000, None)
        self.assertNotIn("tc qdisc del dev eth0 ingress", _joined(host))
        self.assertFalse(host.ifb_exists)

    def test_remove_reports_root_qdisc_restoration_failure(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        host.fail_substr = "tc qdisc del dev eth0 root"
        with self.assertRaisesRegex(ShaperError, "forced failure"):
            shaper.remove(42)
        self.assertIn(42, shaper.shares)
        self.assertTrue(shaper.upload_active)
        self.assertNotIn("tc qdisc add dev eth0 root fq_codel", _joined(host))
        with self.assertRaisesRegex(ShaperError, "qdisc.*restore"):
            shaper.apply(42, None, 20, None, 100_000_000)
        host.fail_substr = None
        shaper.remove(42)
        self.assertEqual(shaper.shares, {})

    def test_leaf_cleanup_ignores_cgroup_control_files(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        control = "/sys/fs/cgroup/bandctl/cgroup.procs"
        host.files[control] = ""
        rmdir = host.rmdir

        def remove_directory(path):
            if path == control:
                raise NotADirectoryError(path)
            rmdir(path)

        with patch.object(host, "rmdir", side_effect=remove_directory):
            notes = shaper.teardown()
        self.assertEqual(notes, [])
        self.assertNotIn("/run/bandctl.json", host.files)

    def test_leaf_cleanup_does_not_move_a_process_that_left_the_leaf(self):
        shaper, host = _shaper()
        leaf = "/sys/fs/cgroup/bandctl/bandctl-42"
        host.dirs.update({"/sys/fs/cgroup/bandctl", leaf})
        host.files[leaf + "/cgroup.procs"] = "43\n"
        host.files["/proc/43/cgroup"] = "0::/other.slice\n"

        self.assertEqual(shaper._remove_tree(), [])
        self.assertNotIn("/sys/fs/cgroup/cgroup.procs", host.files)

    def test_leaf_cleanup_keeps_a_leaf_when_process_cgroup_is_unreadable(self):
        shaper, host = _shaper()
        leaf = "/sys/fs/cgroup/bandctl/bandctl-42"
        host.dirs.update({"/sys/fs/cgroup/bandctl", leaf})
        host.files[leaf + "/cgroup.procs"] = "43\n"
        read = host.read

        def unreadable(path):
            if path == "/proc/43/cgroup":
                raise PermissionError("access denied")
            return read(path)

        with patch.object(host, "read", side_effect=unreadable):
            errors = shaper._remove_tree()
        self.assertTrue(any("access denied" in error for error in errors))
        self.assertIn(leaf, host.dirs)
        self.assertNotIn("/sys/fs/cgroup/cgroup.procs", host.files)

    def test_leaf_cleanup_skips_a_process_that_exited_after_the_listing(self):
        shaper, host = _shaper()
        leaf = "/sys/fs/cgroup/bandctl/bandctl-42"
        host.dirs.update({"/sys/fs/cgroup/bandctl", leaf})
        host.files[leaf + "/cgroup.procs"] = "44\n"

        self.assertEqual(shaper._remove_tree(), [])
        self.assertNotIn(leaf, host.dirs)
        self.assertNotIn("/sys/fs/cgroup/cgroup.procs", host.files)

    def test_last_remove_keeps_state_for_failed_child_cleanup(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        leaf = "/sys/fs/cgroup/bandctl/bandctl-42"
        host.files[leaf + "/cgroup.procs"] = "43\n"
        host.files["/proc/43/cgroup"] = "0::/bandctl/bandctl-42\n"
        rmdir = host.rmdir
        write = host.write

        def busy_leaf(path):
            if path == leaf:
                raise OSError("cgroup is busy")
            return rmdir(path)

        def fail_child_move(path, data):
            if path == "/sys/fs/cgroup/cgroup.procs":
                raise OSError("root move denied")
            return write(path, data)

        with patch.object(host, "rmdir", side_effect=busy_leaf), \
             patch.object(host, "write", side_effect=fail_child_move):
            shaper.remove(42)
            self.assertIn("/run/bandctl.json", host.files)
            notes = shaper.teardown()
            self.assertTrue(any("root move denied" in note for note in notes))
            self.assertIn("/run/bandctl.json", host.files)

    def test_failed_leaf_cleanup_keeps_state_and_retries(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        leaf = "/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs"
        host.files[leaf] = "43\n"  # A child inherited the leaf cgroup.
        host.files["/proc/43/cgroup"] = "0::/bandctl/bandctl-42\n"
        root = "/sys/fs/cgroup/cgroup.procs"
        write = host.write

        def fail_root(path, data):
            if path == root:
                raise OSError("root move denied")
            write(path, data)

        with patch.object(host, "write", side_effect=fail_root):
            notes = shaper.teardown()
            self.assertTrue(any("root move denied" in note for note in notes))
            self.assertIn("/run/bandctl.json", host.files)
            retry = Shaper(iface="eth0", host=host, state_path="/run/bandctl.json")
            retry.recover()
            self.assertIn("root move denied", retry.recovery_error)
            self.assertIn("/run/bandctl.json", host.files)

        retry = Shaper(iface="eth0", host=host, state_path="/run/bandctl.json")
        retry.recover()
        self.assertIsNone(retry.recovery_error)
        self.assertEqual(host.files[root], "43\n")
        self.assertNotIn("/run/bandctl.json", host.files)

    def test_failed_process_restore_keeps_original_cgroup_for_recovery(self):
        shaper, host = _shaper()
        host.files["/proc/42/stat"] = "42 (curl) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        host.files["/proc/42/cgroup"] = "0::/bandctl/bandctl-42\n"
        leaf = "/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs"
        host.files[leaf] = "42\n"
        original = "/sys/fs/cgroup/user.slice/user-1000.slice/session-1.scope/cgroup.procs"
        write = host.write

        def fail_original(path, data):
            if path == original:
                raise OSError("restore denied")
            write(path, data)

        with patch.object(host, "write", side_effect=fail_original):
            notes = shaper.teardown()
            self.assertTrue(any("restore denied" in note for note in notes))
            self.assertIn("/run/bandctl.json", host.files)
            self.assertNotIn("/sys/fs/cgroup/cgroup.procs", host.files)
            again = Shaper(iface="eth0", host=host, state_path="/run/bandctl.json")
            again.recover()
            self.assertIn("restore denied", again.recovery_error)
            self.assertIn("/run/bandctl.json", host.files)
            self.assertNotIn("/sys/fs/cgroup/cgroup.procs", host.files)

        retry = Shaper(iface="eth0", host=host, state_path="/run/bandctl.json")
        retry.recover()
        self.assertIsNone(retry.recovery_error)
        self.assertEqual(host.files[original], "42\n")
        self.assertNotIn("/run/bandctl.json", host.files)

    def test_teardown_reports_failed_qdisc_restore_and_keeps_recovery_state(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        host.fail_substr = "tc qdisc add dev eth0 root"
        notes = shaper.teardown()
        self.assertTrue(any("forced failure" in note for note in notes))
        self.assertIn("/run/bandctl.json", host.files)
        host.fail_substr = None
        again = Shaper(iface="eth0", host=host, state_path="/run/bandctl.json")
        again.recover()
        self.assertIn("tc qdisc add dev eth0 root fq_codel", _joined(host))
        self.assertNotIn("/run/bandctl.json", host.files)

    def test_failed_recovery_preserves_state_and_disables_new_limits(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        host.fail_substr = "tc qdisc add dev eth0 root"
        again = Shaper(iface="eth0", host=host, state_path="/run/bandctl.json")
        notes = again.recover()
        self.assertTrue(any("forced failure" in note for note in notes))
        self.assertIn("/run/bandctl.json", host.files)
        with self.assertRaisesRegex(ShaperError, "shut down"):
            again.apply(43, None, 10, None, 100_000_000)
        host.fail_substr = None
        retry = Shaper(iface="eth0", host=host, state_path="/run/bandctl.json")
        retry.recover()
        self.assertNotIn("/run/bandctl.json", host.files)

    def test_failed_state_replace_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("previous state")
            shaper = Shaper("eth0", host=LinuxHost(), state_path=str(path))
            shaper._shares[42] = Share(42, None, 10, 10, "/original", start_ticks=100)
            with patch("os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(ShaperError, "cannot write recovery state"):
                    shaper._write_state(required=True)
            self.assertEqual(path.read_text(), "previous state")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_state_write_is_private_and_preserves_old_file_on_flush_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            shaper = Shaper("eth0", host=LinuxHost(), state_path=str(path))
            shaper._shares[42] = Share(42, None, 10, 10, "/original", start_ticks=100)
            shaper._write_state(required=True)
            original = path.read_text()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(original)["pids"]["42"], "/original")
            with patch("os.fsync", side_effect=OSError("flush failed")):
                with self.assertRaisesRegex(ShaperError, "cannot write recovery state"):
                    shaper._write_state(required=True)
            self.assertEqual(path.read_text(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_unrestorable_original_cgroup_is_rejected_before_move(self):
        for original in ("", "/../other", "/a/../other", "relative", "/bad\x00path", "/bad\ud800path"):
            with self.subTest(original=original):
                shaper, host = _shaper()
                host.files["/proc/42/cgroup"] = f"0::{original}\n"
                with self.assertRaisesRegex(ShaperError, "invalid cgroup"):
                    shaper.apply(42, None, 10, None, 100_000_000)
                self.assertEqual(shaper.shares, {})
                self.assertNotIn("/run/bandctl.json", host.files)
                self.assertFalse(any(path.startswith("/sys/fs/cgroup/bandctl") for path in host.files))
                self.assertEqual(host.commands, [])

    def test_non_utf8_original_cgroup_is_rejected_before_move(self):
        shaper, host = _shaper()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cgroup"
            path.write_bytes(b"0::/session-\xff.scope\n")
            read = host.read

            def read_cgroup(source):
                return LinuxHost().read(str(path)) if source == "/proc/42/cgroup" else read(source)

            with patch.object(host, "read", side_effect=read_cgroup):
                with self.assertRaisesRegex(ShaperError, "invalid cgroup"):
                    shaper.apply(42, None, 10, None, 100_000_000)
            self.assertEqual(host.commands, [])
            self.assertEqual(shaper.shares, {})
            self.assertNotIn("/run/bandctl.json", host.files)

    def test_recovery_rejects_non_utf8_cgroup_without_changing_host(self):
        shaper, host = _shaper()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_bytes(b'{"iface":"eth0","pids":{"42":"/session-\xff.scope"}}')
            read = host.read

            def read_state(source):
                return LinuxHost().read(str(path)) if source == "/run/bandctl.json" else read(source)

            with patch.object(host, "read", side_effect=read_state):
                shaper.recover()
            self.assertIn("invalid pid or cgroup", shaper.recovery_error)
            self.assertEqual(host.commands, [])
            self.assertTrue(path.exists())

    def test_valid_replacement_character_in_cgroup_path_is_preserved(self):
        shaper, host = _shaper()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cgroup"
            path.write_text("0::/session-\ufffd.scope\n", encoding="utf-8")
            read = host.read

            def read_cgroup(source):
                return LinuxHost().read(str(path)) if source == "/proc/42/cgroup" else read(source)

            with patch.object(host, "read", side_effect=read_cgroup):
                shaper.apply(42, None, 10, None, 100_000_000)
            self.assertEqual(json.loads(host.files["/run/bandctl.json"])["pids"]["42"], "/session-\ufffd.scope")

    def test_original_cgroup_ignores_colons_in_legacy_controller_path(self):
        shaper, host = _shaper()
        host.files["/proc/42/cgroup"] = "4:cpu:/legacy::name\n0::/user.slice/session.scope\n"

        shaper.apply(42, None, 10, None, 100_000_000)
        state = json.loads(host.files["/run/bandctl.json"])
        self.assertEqual(state["pids"]["42"], "/user.slice/session.scope")
        shaper.remove(42)
        self.assertEqual(host.files["/sys/fs/cgroup/user.slice/session.scope/cgroup.procs"], "42\n")

    def test_original_cgroup_with_unicode_line_separator_is_restored(self):
        shaper, host = _shaper()
        host.files["/proc/42/cgroup"] = "0::/user.slice/session\u2028scope\n"

        shaper.apply(42, None, 10, None, 100_000_000)
        state = json.loads(host.files["/run/bandctl.json"])
        self.assertEqual(state["pids"]["42"], "/user.slice/session\u2028scope")
        shaper.remove(42)
        self.assertEqual(host.files["/sys/fs/cgroup/user.slice/session\u2028scope/cgroup.procs"], "42\n")

    def test_original_cgroup_with_trailing_space_is_restored(self):
        shaper, host = _shaper()
        host.files["/proc/42/cgroup"] = "0::/user.slice/session.scope \n"

        shaper.apply(42, None, 10, None, 100_000_000)
        state = json.loads(host.files["/run/bandctl.json"])
        self.assertEqual(state["pids"]["42"], "/user.slice/session.scope ")
        shaper.remove(42)
        self.assertEqual(host.files["/sys/fs/cgroup/user.slice/session.scope /cgroup.procs"], "42\n")

    def test_records_original_cgroup_before_moving_process(self):
        shaper, host = _shaper()
        host.files["/proc/42/stat"] = "42 (curl) S " + "0 " * 18 + "100"
        original_write = host.write
        recorded = []

        def inspect_state(path, data):
            if path == "/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs":
                recorded.append(json.loads(host.files["/run/bandctl.json"]))
            original_write(path, data)

        host.write = inspect_state
        shaper.apply(42, 10, None, 100_000_000, None, expected_start_ticks=100)
        self.assertEqual(recorded[0]["pids"]["42"], "/user.slice/user-1000.slice/session-1.scope")
        self.assertEqual(recorded[0]["start_ticks"]["42"], 100)

    def test_refuses_to_move_process_without_recovery_state(self):
        shaper, host = _shaper()
        original_write = host.write
        moved = []

        def fail_state(path, data):
            if path == "/run/bandctl.json":
                raise OSError("state is read only")
            if path == "/sys/fs/cgroup/bandctl/bandctl-42/cgroup.procs":
                moved.append(data)
            original_write(path, data)

        host.write = fail_state
        with self.assertRaisesRegex(ShaperError, "recovery state"):
            shaper.apply(42, 10, None, 100_000_000, None)
        self.assertEqual(moved, [])
        self.assertEqual(shaper.shares, {})

    def test_refuses_qdisc_replacement_without_recovery_state(self):
        shaper, host = _shaper()
        shaper.apply(42, 10, None, 100_000_000, None)
        original_write = host.write
        host.commands.clear()

        def fail_state(path, data):
            if path == "/run/bandctl.json":
                raise ShaperError("state is read only")
            original_write(path, data)

        host.write = fail_state
        with self.assertRaisesRegex(ShaperError, "recovery state"):
            shaper.apply(42, 10, 10, 100_000_000, 100_000_000)
        self.assertFalse(any("qdisc replace dev eth0 root" in cmd for cmd in _joined(host)))
        self.assertEqual(shaper.shares[42].up_pct, None)

    def test_update_rejects_a_new_process_with_an_old_share(self):
        shaper, host = _shaper()
        host.files["/proc/42/stat"] = "42 (curl) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        host.files["/proc/42/stat"] = "42 (curl) S " + "0 " * 18 + "200"
        with self.assertRaisesRegex(ShaperError, "changed"):
            shaper.apply(42, None, 20, None, 100_000_000, expected_start_ticks=200)
        self.assertEqual(shaper.shares[42].up_pct, 10)

    def test_failed_apply_keeps_process_tracked_when_restore_fails(self):
        shaper, host = _shaper()
        host.fail_substr = "nft -f"
        original_write = host.write

        def fail_restore(path, data):
            if path.endswith("session-1.scope/cgroup.procs"):
                raise OSError("restore failed")
            original_write(path, data)

        host.write = fail_restore
        with self.assertRaisesRegex(ShaperError, "rollback incomplete.*could not restore pid 42"):
            shaper.apply(42, None, 10, None, 100_000_000)
        self.assertIn(42, shaper.shares)
        self.assertIn('"42"', host.files["/run/bandctl.json"])
        host.write = original_write
        host.fail_substr = None
        shaper.remove(42)
        self.assertEqual(shaper.shares, {})
        self.assertEqual(host.files["/sys/fs/cgroup/user.slice/user-1000.slice/session-1.scope/cgroup.procs"], "42\n")

    def test_failed_apply_removes_the_leaf_it_made(self):
        shaper, host = _shaper()
        host.fail_substr = "nft -f"
        with self.assertRaises(ShaperError):
            shaper.apply(42, None, 10, None, 100_000_000)
        self.assertNotIn("/sys/fs/cgroup/bandctl/bandctl-42", host.dirs)

    def test_failed_change_keeps_the_existing_leaf(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        host.fail_substr = "nft -f"
        with self.assertRaises(ShaperError):
            shaper.apply(42, None, 20, None, 100_000_000)
        self.assertIn("/sys/fs/cgroup/bandctl/bandctl-42", host.dirs)
        self.assertEqual(shaper.shares[42].up_pct, 10)

    def test_teardown_leaves_an_untouched_qdisc_alone(self):
        shaper, host = _shaper()
        host.qdisc = "qdisc cake 8001: root refcnt 2 bandwidth unlimited\n"
        shaper.apply(42, 10, None, 100_000_000, None)
        host.fail_substr = "nft delete table"

        def no_permission(args, input_text=None):
            host.commands.append((list(args), input_text))
            if args[0] == "nft":
                raise ShaperError("Error: Could not process rule: Operation not permitted", args)
            return FakeHost.run(host, args, input_text)

        host.run = no_permission  # type: ignore[method-assign]
        notes = shaper.teardown()
        self.assertTrue(any("Operation not permitted" in note for note in notes))
        commands = _joined(host)
        self.assertFalse(any("qdisc del dev eth0 root" in cmd for cmd in commands))
        self.assertFalse(any("qdisc add dev eth0 root" in cmd for cmd in commands))

    def test_teardown_still_restores_a_qdisc_it_replaced(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        host.fail_substr = "nft delete table"
        shaper.teardown()
        self.assertTrue(any("qdisc add dev eth0 root fq_codel" in cmd for cmd in _joined(host)))

    def test_remove_keeps_the_share_when_restore_fails(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        original_write = host.write

        def busy(path: str, data: str) -> None:
            if path.endswith("session-1.scope/cgroup.procs"):
                raise ShaperError(f"{path}: Device or resource busy")
            original_write(path, data)

        host.write = busy  # type: ignore[method-assign]
        with self.assertRaises(ShaperError) as caught:
            shaper.remove(42)
        self.assertIn("could not restore pid 42", str(caught.exception))
        self.assertIn(42, shaper.shares)
        self.assertTrue(shaper.upload_active)
        # Once the write works again, remove finishes normally.
        host.write = original_write  # type: ignore[method-assign]
        self.assertIn("removed", shaper.remove(42))
        self.assertEqual(shaper.shares, {})

    def test_remove_does_not_move_a_process_that_left_its_leaf(self):
        shaper, host = _shaper()
        host.files["/proc/42/stat"] = "42 (curl) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        host.files["/proc/42/cgroup"] = "0::/new.slice\n"

        shaper.remove(42)

        original = "/sys/fs/cgroup/user.slice/user-1000.slice/session-1.scope/cgroup.procs"
        self.assertNotIn(original, host.files)
        self.assertFalse(shaper.shares)

    def test_restore_rechecks_identity_after_reading_cgroup(self):
        for operation in ("remove", "teardown"):
            with self.subTest(operation=operation):
                shaper, host = _shaper()
                stat = "/proc/42/stat"
                host.files[stat] = "42 (old) S " + "0 " * 18 + "100"
                shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
                read = host.read

                def replace_after_cgroup(path):
                    value = read(path)
                    if path == "/proc/42/cgroup":
                        host.files[stat] = "42 (new) S " + "0 " * 18 + "200"
                    return value

                with patch.object(host, "read", side_effect=replace_after_cgroup):
                    if operation == "remove":
                        with self.assertRaisesRegex(ShaperError, "cannot verify pid 42"):
                            shaper.remove(42)
                        self.assertIn(42, shaper.shares)
                    else:
                        self.assertTrue(any("cannot verify pid 42" in note for note in shaper.teardown()))
                original = "/sys/fs/cgroup/user.slice/user-1000.slice/session-1.scope/cgroup.procs"
                self.assertNotIn(original, host.files)
                self.assertIn("/run/bandctl.json", host.files)

    def test_teardown_does_not_move_a_process_that_left_its_leaf(self):
        shaper, host = _shaper()
        host.files["/proc/42/stat"] = "42 (curl) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        host.files["/proc/42/cgroup"] = "0::/new.slice\n"

        self.assertEqual(shaper.teardown(), [])

        original = "/sys/fs/cgroup/user.slice/user-1000.slice/session-1.scope/cgroup.procs"
        self.assertNotIn(original, host.files)
        self.assertEqual(host.files["/proc/42/cgroup"], "0::/new.slice\n")

    def test_remove_keeps_a_share_when_its_cgroup_cannot_be_read(self):
        shaper, host = _shaper()
        host.files["/proc/42/stat"] = "42 (curl) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        read = host.read

        def inaccessible(path):
            if path == "/proc/42/cgroup":
                raise PermissionError("cgroup read denied")
            return read(path)

        with patch.object(host, "read", side_effect=inaccessible):
            with self.assertRaisesRegex(ShaperError, "cgroup read denied"):
                shaper.remove(42)
        self.assertIn(42, shaper.shares)

    def test_remove_keeps_share_when_process_liveness_is_unreadable(self):
        shaper, host = _shaper()
        host.files["/proc/42/stat"] = "42 (curl) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        state = host.files["/run/bandctl.json"]
        read = host.read

        def inaccessible(path):
            if path in ("/proc/42/stat", "/proc/42/cgroup"):
                raise PermissionError("read denied")
            return read(path)

        with patch.object(host, "read", side_effect=inaccessible):
            with self.assertRaisesRegex(ShaperError, "cannot verify pid 42"):
                shaper.remove(42)

        self.assertIn(42, shaper.shares)
        self.assertTrue(shaper.upload_active)
        self.assertEqual(host.files["/run/bandctl.json"], state)
        self.assertIn("removed", shaper.remove(42))

    def test_remove_drops_a_share_whose_process_exited(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        del host.files["/proc/42/cgroup"]
        original_write = host.write

        def gone(path: str, data: str) -> None:
            if path.endswith("cgroup.procs"):
                raise ShaperError(f"{path}: No such process")
            original_write(path, data)

        host.write = gone  # type: ignore[method-assign]
        self.assertIn("removed", shaper.remove(42))
        self.assertEqual(shaper.shares, {})
        self.assertFalse(shaper.upload_active)


def _open_socket(host: FakeHost, pid: int = 42) -> None:
    host.files["/proc/net/tcp"] = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0800000A:9C40 04030201:01BB 01 00000000:00000000 00:00000000 00000000     0        0 99 1 0000000000000000 100 0 0 10 0\n"
        "   1: 0100007F:01BB 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 55 1 0000000000000000 100 0 0 10 0\n"
    )
    host.links[f"/proc/{pid}/fd/3"] = "socket:[99]"
    host.links[f"/proc/{pid}/fd/4"] = "socket:[55]"


if __name__ == "__main__":
    unittest.main()
