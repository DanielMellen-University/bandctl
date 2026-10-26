"""Process scan against a fake /proc tree, plus a smoke read of the real one."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from bandctl.procscan import (
    format_cmdline,
    parse_proc_net,
    parse_start_ticks,
    parse_socket_inodes,
    scan_processes,
    user_from_status,
)


TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:0277 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 555 1 0000000000000000 10
   1: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 777 1 0000000000000000 10
"""


class ParseTests(unittest.TestCase):
    def test_start_ticks_with_spaces_and_parentheses_in_name(self):
        text = "42 (worker ) name) S " + "0 " * 18 + "12345 0 0\n"
        self.assertEqual(parse_start_ticks(text), 12345)
        self.assertIsNone(parse_start_ticks("42 (bad) S 0"))
        self.assertIsNone(parse_start_ticks("42 (bad) S " + "0 " * 18 + "oops"))

    def test_inodes(self):
        self.assertEqual(parse_socket_inodes(TCP), {"555", "777"})

    def test_tcp_flows_skip_listeners(self):
        text = (
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            "   0: 0800000A:9C40 04030201:01BB 01 00000000:00000000 00:00000000 00000000     0        0 99 1 0000000000000000 100 0 0 10 0\n"
            "   1: 0100007F:01BB 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 55 1 0000000000000000 100 0 0 10 0\n"
        )
        flows = parse_proc_net(text, "tcp", "ip")
        self.assertEqual(set(flows), {"99"})
        flow = flows["99"]
        self.assertEqual(flow.proto, "tcp")
        self.assertEqual(flow.family, "ip")
        self.assertEqual((flow.local_ip, flow.local_port), ("10.0.0.8", 40000))
        self.assertEqual((flow.remote_ip, flow.remote_port), ("1.2.3.4", 443))

    def test_unconnected_udp_has_no_remote(self):
        text = (
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            "   0: 0800000A:0035 00000000:0000 07 00000000:00000000 00:00000000 00000000     0        0 12 1 0000000000000000 100 0 0 10 0\n"
        )
        flow = parse_proc_net(text, "udp", "ip")["12"]
        self.assertEqual(flow.proto, "udp")
        self.assertEqual((flow.local_ip, flow.local_port), ("10.0.0.8", 53))
        self.assertEqual(flow.remote_port, 0)
        self.assertEqual(flow.remote_ip, "")

    def test_ipv6_and_v4_mapped(self):
        # ::1 port 80 to ::1 port 443. Each 32-bit word is little-endian in the file.
        text = (
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            "   0: 00000000000000000000000001000000:0050 00000000000000000000000001000000:01BB 01 00000000:00000000 00:00000000 00000000 0 0 77 1 0000000000000000 100 0 0 10 0\n"
            "   1: 0000000000000000FFFF00000800000A:9C40 0000000000000000FFFF00000100007F:01BB 01 00000000:00000000 00:00000000 00000000 0 0 88 1 0000000000000000 100 0 0 10 0\n"
        )
        flows = parse_proc_net(text, "tcp", "ipv6")
        self.assertEqual(flows["77"].family, "ipv6")
        self.assertEqual(flows["77"].local_ip, "::1")
        self.assertEqual(flows["77"].local_port, 80)
        self.assertEqual(flows["77"].remote_port, 443)
        # ::ffff:10.0.0.8 and ::ffff:127.0.0.1 are IPv4 packets on the wire.
        mapped = flows["88"]
        self.assertEqual(mapped.family, "ip")
        self.assertEqual(mapped.local_ip, "10.0.0.8")
        self.assertEqual(mapped.local_port, 40000)
        self.assertEqual(mapped.remote_ip, "127.0.0.1")
        self.assertEqual(mapped.remote_port, 443)

    def test_cmdline(self):
        self.assertEqual(format_cmdline(b"curl\0https://example\0", "curl"), "curl https://example")
        self.assertEqual(format_cmdline(b"", "sshd"), "sshd")
        long = format_cmdline(b"x" * 200, "x", limit=10)
        self.assertEqual(long, "xxxxxxx...")

    def test_user(self):
        self.assertEqual(user_from_status("Uid:\t0\t0\t0\t0\n"), "root")
        self.assertEqual(user_from_status("Name:\tnobody\n", fallback_uid=999999), "999999")


class ScanTests(unittest.TestCase):
    def test_fake_proc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            net = root / "net"
            net.mkdir()
            (net / "tcp").write_text(TCP, encoding="utf-8")
            self._pid(root, 7, "curl", b"curl\0https://example\0", "Uid:\t1000\t1000\t1000\t1000\n", "555")
            self._pid(root, 8, "bash", b"bash\0", "Uid:\t1000\t1000\t1000\t1000\n", None)
            found = {item.pid: item for item in scan_processes(root, skip_pid=0)}
        self.assertEqual(found[7].sockets, 1)
        self.assertEqual(found[7].cmdline, "curl https://example")
        self.assertEqual(found[7].comm, "curl")
        self.assertEqual(found[7].start_ticks, 12345)
        self.assertEqual(found[8].sockets, 0)

    def test_live_proc_skips_self(self):
        found = scan_processes()
        self.assertTrue(found)
        self.assertNotIn(os.getpid(), {item.pid for item in found})
        self.assertTrue(all(item.pid > 0 and item.comm for item in found))

    def _pid(self, root: Path, pid: int, comm: str, cmdline: bytes, status: str, inode: str | None) -> None:
        entry = root / str(pid)
        fd = entry / "fd"
        fd.mkdir(parents=True)
        (entry / "comm").write_text(comm + "\n", encoding="utf-8")
        (entry / "cmdline").write_bytes(cmdline)
        (entry / "status").write_text(status, encoding="utf-8")
        (entry / "stat").write_text(f"{pid} ({comm}) S " + "0 " * 18 + "12345 0\n", encoding="utf-8")
        if inode is not None:
            os.symlink(f"socket:[{inode}]", fd / "3")


if __name__ == "__main__":
    unittest.main()
