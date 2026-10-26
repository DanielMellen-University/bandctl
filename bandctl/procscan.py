"""Process list, with a count of sockets owned by each pid.

Socket owners come from ``/proc/net/{tcp,tcp6,udp,udp6}`` inodes matched
against ``/proc/<pid>/fd``. A pid that disappears mid-scan is skipped.
"""

from __future__ import annotations

import os
import pwd
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    comm: str
    cmdline: str
    user: str
    sockets: int


_UID_RE = re.compile(r"^Uid:\s+(\d+)", re.MULTILINE)


def parse_socket_inodes(text: str) -> set[str]:
    """Inode column of a ``/proc/net/tcp``-style table (the 10th field)."""
    inodes: set[str] = set()
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 10 and parts[9].isdigit():
            inodes.add(parts[9])
    return inodes


def format_cmdline(raw: bytes, comm: str, limit: int = 80) -> str:
    text = raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
    if not text:
        text = comm
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def user_from_status(status_text: str, fallback_uid: int | None = None) -> str:
    match = _UID_RE.search(status_text)
    uid = int(match.group(1)) if match else fallback_uid
    if uid is None:
        return "?"
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def scan_processes(proc_root: str | Path = "/proc", *, skip_pid: int | None = None) -> list[ProcessInfo]:
    """Every userspace pid we can read, except *skip_pid* (this process, by default)."""
    root = Path(proc_root)
    if skip_pid is None:
        skip_pid = os.getpid()
    inodes = _collect_inodes(root)
    found: list[ProcessInfo] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == skip_pid:
            continue
        info = _one(entry, inodes)
        if info is not None:
            found.append(info)
    return found


def _collect_inodes(root: Path) -> set[str]:
    inodes: set[str] = set()
    net = root / "net"
    for name in ("tcp", "tcp6", "udp", "udp6"):
        path = net / name
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        inodes |= parse_socket_inodes(text)
    return inodes


def _one(entry: Path, inodes: set[str]) -> ProcessInfo | None:
    try:
        comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip() or "?"
        cmdline = format_cmdline((entry / "cmdline").read_bytes(), comm)
        status = (entry / "status").read_text(encoding="utf-8", errors="replace")
        uid = entry.stat().st_uid
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    sockets = _socket_count(entry / "fd", inodes)
    return ProcessInfo(
        pid=int(entry.name),
        comm=comm,
        cmdline=cmdline,
        user=user_from_status(status, uid),
        sockets=sockets,
    )


def _socket_count(fd_dir: Path, inodes: set[str]) -> int:
    if not inodes:
        return 0
    try:
        names = list(fd_dir.iterdir())
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return 0
    count = 0
    for fd in names:
        try:
            target = os.readlink(fd)
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            continue
        if not target.startswith("socket:["):
            continue
        inode = target[8:-1]
        if inode in inodes:
            count += 1
    return count
