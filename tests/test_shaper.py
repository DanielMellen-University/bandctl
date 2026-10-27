"""Shaper plans and rollback against a fake host. No root, no real tc."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from bandctl.groups import Group
from bandctl.rates import htb_plan, percent_to_bps
from bandctl.procscan import SockFlow
from bandctl.shaper import (
    IFB,
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
        self.files: dict[str, str] = {}
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

    def write(self, path: str, data: str) -> None:
        self.files[path] = data

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

    def test_nft_matches_leaf_name(self):
        script = nft_script([Share(42, 30, None, 10, "/user.slice", "curl")])
        self.assertIn('socket cgroupv2 level 2 "bandctl-42"', script)
        self.assertIn("ct mark set 10 meta mark set 10", script)
        self.assertIn("hook output", script)
        self.assertIn("hook prerouting", script)


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
            shaper._sample_at = 1.0
            shaper._bytes[("up", 10)] = 500
            self.assertEqual(shaper.sample(), {42: (0.0, 4000.0), 43: (0.0, 4000.0)})


class ApplyTests(unittest.TestCase):
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
        with patch.object(shaper, "_sync", side_effect=ShaperError("sync failed")):
            with patch.object(host, "write", wraps=host.write) as write:
                with self.assertRaises(ShaperError):
                    shaper.remove(42)
                write.assert_not_called()
        self.assertIn(42, shaper.shares)

    def test_sample_retires_reused_pid_without_moving_replacement(self):
        shaper, host = _shaper()
        stat = "/proc/42/stat"
        host.files[stat] = "42 (steam) S " + "0 " * 18 + "100"
        shaper.apply(42, None, 10, None, 100_000_000, expected_start_ticks=100)
        host.files[stat] = "42 (other) S " + "0 " * 18 + "200"
        original = "/sys/fs/cgroup/user.slice/user-1000.slice/session-1.scope/cgroup.procs"
        self.assertNotIn(original, host.files)
        shaper.sample()
        self.assertNotIn(42, shaper.shares)
        self.assertNotIn(original, host.files)
        self.assertFalse(shaper.upload_active)

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
        self.assertNotIn("/run/bandctl.json", host.files)

    def test_sample_bits_per_second(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 50, None, 100_000_000)
        host.class_show["eth0"] = "class htb 1:10 parent 1:1\n Sent 0 bytes 0 pkt\n"
        self.assertEqual(shaper.sample()[42][1], 0.0)
        host.class_show["eth0"] = "class htb 1:10 parent 1:1\n Sent 1000000 bytes 8 pkt\n"
        shaper._sample_at = time.monotonic() - 1
        _down, up = shaper.sample()[42]
        self.assertAlmostEqual(up, 8_000_000, delta=80_000)

    def test_teardown_is_idempotent(self):
        shaper, host = _shaper()
        shaper.apply(42, 10, 10, 100_000_000, 100_000_000)
        shaper.teardown()
        shaper.teardown()
        self.assertFalse(host.ifb_exists)
        self.assertNotIn("bandctl-42", host.dirs)

    def test_recover_uses_state_file(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        # A new process, same fake machine, as after a crash.
        again, _same = _shaper(host)
        notes = again.recover()
        self.assertTrue(any("restored pid 42" in note for note in notes))
        self.assertTrue(any("qdisc add dev eth0 root fq_codel" in cmd for cmd in _joined(host)))
        self.assertNotIn("/run/bandctl.json", host.files)

    def test_restore_reports_a_cgroup_write_error(self):
        shaper, host = _shaper()

        def boom(path: str, data: str) -> None:
            raise ShaperError(f"{path}: Device or resource busy")

        host.write = boom  # type: ignore[method-assign]
        with self.assertRaises(ShaperError) as caught:
            shaper._restore_pid(42, "/user.slice/session-1.scope")
        self.assertIn("could not restore pid 42", str(caught.exception))

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

    def test_upload_flower_does_not_touch_ingress(self):
        shaper, host = _shaper()
        _open_socket(host)
        shaper.apply(42, None, 20, None, 100_000_000)
        commands = _joined(host)
        self.assertTrue(any("flower" in cmd and "classid 1:10" in cmd for cmd in commands))
        self.assertFalse(any("parent ffff:" in cmd for cmd in commands))
        self.assertFalse(any(cmd.startswith("modprobe ifb") for cmd in commands))
        self.assertTrue(any("modprobe cls_flower" in cmd for cmd in commands))

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

    def test_remove_drops_a_share_whose_process_exited(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        del host.files["/proc/42/cgroup"]

        def gone(path: str, data: str) -> None:
            raise ShaperError(f"{path}: No such process")

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
