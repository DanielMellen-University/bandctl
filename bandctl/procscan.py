"""Process list, with a count of sockets owned by each pid.

Socket owners come from ``/proc/net/{tcp,tcp6,udp,udp6}`` inodes matched
against ``/proc/<pid>/fd``. A pid that disappears mid-scan is skipped.
"""

from __future__ import annotations

import ipaddress
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
    start_ticks: int | None = None


@dataclass(frozen=True)
class SockFlow:
    """One socket a flower filter can match.

    ``family`` is ``ip`` or ``ipv6`` after IPv4-mapped addresses are unwrapped,
    so the filter matches the packets on the wire. ``remote_port`` 0 means the
    socket is not connected (typical UDP). Listeners are not represented.
    """

    proto: str
    family: str
    local_ip: str
    local_port: int
    remote_ip: str
    remote_port: int


_UID_RE = re.compile(r"^Uid:\s+(\d+)", re.MULTILINE)


def parse_start_ticks(text: str) -> int | None:
    """Read field 22 from /proc/<pid>/stat, including names with parentheses."""
    end = text.rfind(")")
    if end < 0:
        return None
    fields = text[end + 1 :].split()
    if len(fields) < 20:
        return None
    try:
        value = int(fields[19])
    except ValueError:
        return None
    return value if value >= 0 else None


def parse_socket_inodes(text: str) -> set[str]:
    """Inode column of a ``/proc/net/tcp``-style table (the 10th field)."""
    inodes: set[str] = set()
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 10 and parts[9].isdigit():
            inodes.add(parts[9])
    return inodes


def parse_proc_net(text: str, proto: str, family: str) -> dict[str, SockFlow]:
    """Inode to flow for one ``/proc/net`` table.

    ``proto`` is ``tcp`` or ``udp``. ``family`` is ``ip`` or ``ipv6``, matching
    the file the text came from. TCP listeners (remote port 0) are skipped.
    A row whose two ends are different families is skipped too.
    """
    found: dict[str, SockFlow] = {}
    if proto not in ("tcp", "udp") or family not in ("ip", "ipv6"):
        raise ValueError(f"unsupported table {proto}/{family}")
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 10 or not parts[9].isdigit():
            continue
        try:
            local_ip, local_port, local_family = _decode_endpoint(parts[1], family)
            remote_ip, remote_port, remote_family = _decode_endpoint(parts[2], family)
        except ValueError:
            continue
        if local_port <= 0:
            continue
        if proto == "tcp" and remote_port <= 0:
            continue
        if remote_port and local_family != remote_family:
            continue
        found[parts[9]] = SockFlow(
            proto=proto,
            family=local_family,
            local_ip=local_ip,
            local_port=local_port,
            remote_ip=remote_ip if remote_port else "",
            remote_port=remote_port,
        )
    return found


def _decode_endpoint(field: str, family: str) -> tuple[str, int, str]:
    """Return ``(address, port, family)``. Mapped IPv6 comes back as ``ip``."""
    ip_hex, port_hex = field.split(":")
    port = int(port_hex, 16)
    if not 0 <= port <= 65535:
        raise ValueError(port_hex)
    if family == "ip":
        raw = bytes.fromhex(ip_hex)
        if len(raw) != 4:
            raise ValueError(ip_hex)
        return ".".join(str(byte) for byte in raw[::-1]), port, "ip"
    if len(ip_hex) != 32:
        raise ValueError(ip_hex)
    words = [bytes.fromhex(ip_hex[index : index + 8])[::-1] for index in range(0, 32, 8)]
    addr = ipaddress.IPv6Address(b"".join(words))
    mapped = addr.ipv4_mapped
    if mapped is not None:
        return str(mapped), port, "ip"
    return str(addr), port, "ipv6"


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
    except (KeyError, OSError, OverflowError):
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
        start_ticks = _start_ticks(entry)
        comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").removesuffix("\n") or "?"
        cmdline = format_cmdline((entry / "cmdline").read_bytes(), comm)
        status = (entry / "status").read_text(encoding="utf-8", errors="replace")
        uid = entry.stat().st_uid
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    sockets = _socket_count(entry / "fd", inodes)
    end_ticks = _start_ticks(entry)
    if start_ticks is not None and end_ticks != start_ticks:
        return None
    return ProcessInfo(
        pid=int(entry.name),
        comm=comm,
        cmdline=cmdline,
        user=user_from_status(status, uid),
        sockets=sockets,
        start_ticks=start_ticks,
    )


def _start_ticks(entry: Path) -> int | None:
    try:
        return parse_start_ticks((entry / "stat").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def _socket_count(fd_dir: Path, inodes: set[str]) -> int:
    if not inodes:
        return 0
    try:
        names = list(fd_dir.iterdir())
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return 0
    sockets: set[str] = set()
    for fd in names:
        try:
            target = os.readlink(fd)
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            continue
        if not (target.startswith("socket:[") and target.endswith("]")):
            continue
        inode = target[8:-1]
        if inode in inodes:
            sockets.add(inode)
    return len(sockets)
