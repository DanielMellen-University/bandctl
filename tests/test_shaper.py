"""Shaper plans and rollback against a fake host. No root, no real tc."""

from __future__ import annotations

import time
import unittest

from bandctl.rates import htb_plan, percent_to_bps
from bandctl.shaper import (
    IFB,
    Share,
    Shaper,
    ShaperError,
    nft_script,
    parse_class_bytes,
    parse_root_qdisc,
)


class FakeHost:
    def __init__(self):
        self.commands: list[tuple[list[str], str | None]] = []
        self.files: dict[str, str] = {}
        self.dirs: set[str] = {"/sys/fs/cgroup"}
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
        for entry in list(self.dirs) + list(self.files):
            if entry.startswith(prefix):
                names.add(entry[len(prefix) :].split("/")[0])
        return sorted(names)

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

    def test_nft_matches_leaf_name(self):
        script = nft_script([Share(42, 30, None, 10, "/user.slice", "curl")])
        self.assertIn('socket cgroupv2 level 2 "bandctl-42"', script)
        self.assertIn("ct mark set 10 meta mark set 10", script)
        self.assertIn("hook output", script)
        self.assertIn("hook prerouting", script)


class ApplyTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
