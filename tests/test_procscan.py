"""Process scan against a fake /proc tree, plus a smoke read of the real one."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from bandctl.procscan import (
    format_cmdline,
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
    def test_inodes(self):
        self.assertEqual(parse_socket_inodes(TCP), {"555", "777"})

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
        if inode is not None:
            os.symlink(f"socket:[{inode}]", fd / "3")


if __name__ == "__main__":
    unittest.main()
