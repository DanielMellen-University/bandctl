"""Per-process TCP rates from ``ss``.

``ss -tinpe`` prints ``bytes_received`` and ``bytes_acked`` for each socket.
The difference over a sample interval is the rate. Loopback sockets are skipped, so
local services do not look like link traffic. The first sample has no
previous total, so it returns no rates.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess

_PID_RE = re.compile(r'\("(?:\\.|[^"\\])*",pid=(\d+)(?=,|\))')
_QUOTED_RE = re.compile(r'"(?:\\.|[^"\\])*"')
_SOCKET_COOKIE_RE = re.compile(r"\bsk:([0-9a-fA-F]+)\b")
_SOCKET_INODE_RE = re.compile(r"\bino:(\d+)\b")
_FIELD_RE = {
    "bytes_received": re.compile(r"bytes_received:(\d+)"),
    "bytes_acked": re.compile(r"bytes_acked:(\d+)"),
    "bytes_sent": re.compile(r"bytes_sent:(\d+)"),
}


def above_share(
    down_bps: float,
    up_bps: float,
    down_denom: float,
    up_denom: float,
    share: float = 0.01,
) -> bool:
    """True when either direction is strictly above ``share`` of its denominator."""
    if down_denom > 0 and down_bps > share * down_denom:
        return True
    if up_denom > 0 and up_bps > share * up_denom:
        return True
    return False


def parse_ss(text: str) -> dict[int, tuple[int, int]]:
    """Map pid to ``(bytes_received, bytes_acked)`` summed across its sockets.

    A socket with no process, or with either end on loopback, is ignored.
    ``bytes_sent`` is used when ``bytes_acked`` is absent.
    """
    totals: dict[int, list[int]] = {}
    pids: list[int] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw[0].isspace():
            if not pids:
                continue
            received = _field(raw, "bytes_received") or 0
            acked = _field(raw, "bytes_acked")
            if acked is None:
                acked = _field(raw, "bytes_sent") or 0
            if received == 0 and acked == 0:
                continue
            for pid in pids:
                slot = totals.setdefault(pid, [0, 0])
                slot[0] += received
                slot[1] += acked
            continue
        pids = _socket_pids(raw)
    return {pid: (received, acked) for pid, (received, acked) in totals.items()}


def parse_ss_sockets(text: str, *, omit_unreported: bool = False) -> dict[tuple[int, str], tuple[int, int]]:
    """Map owned sockets to counters, optionally omitting records without counters."""
    found: dict[tuple[int, str], tuple[int, int]] = {}
    reported: set[tuple[int, str]] = set()
    keys: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if not raw[0].isspace():
            pids = _socket_pids(raw)
            metadata = _QUOTED_RE.sub('""', raw)
            cookie = _SOCKET_COOKIE_RE.search(metadata)
            inode = _SOCKET_INODE_RE.search(metadata)
            local, peer = _endpoints(raw)
            if cookie:
                identity = "sk:" + cookie.group(1).lower()
            elif inode:
                identity = "ino:" + inode.group(1)
            else:
                identity = f"{local} {peer}" if local and peer else ""
            keys = [(pid, identity) for pid in pids] if identity else []
            for key in keys:
                found[key] = (0, 0)
            continue
        received = _field(raw, "bytes_received")
        acked = _field(raw, "bytes_acked")
        if acked is None:
            acked = _field(raw, "bytes_sent")
        if received is None and acked is None:
            continue
        for key in keys:
            reported.add(key)
            previous = found[key]
            found[key] = (previous[0] if received is None else received, previous[1] if acked is None else acked)
    return {key: value for key, value in found.items() if key in reported} if omit_unreported else found


class TrafficSampler:
    """Track socket counters and return per-process rates and session totals."""

    def __init__(self) -> None:
        self._prev: dict[tuple[int, str], tuple[int, int, float]] | None = None
        self._prev_at: float | None = None
        self._totals: dict[int, tuple[int, int]] = {}
        self._seen_pids: dict[int, float] = {}
        self.primed = False

    @property
    def totals(self) -> dict[int, tuple[int, int]]:
        return dict(self._totals)

    def forget_total(self, pid: int) -> None:
        """Discard a process total while retaining its latest socket baselines."""
        self._totals.pop(pid, None)
        self._seen_pids.pop(pid, None)

    def retain_processes(self, live_pids: set[int], now: float) -> None:
        """Keep totals for live processes and discard long-absent ones."""
        for pid in self._totals:
            if pid in live_pids:
                self._seen_pids[pid] = now
        for pid, seen in list(self._seen_pids.items()):
            if now - seen > 300:
                self._seen_pids.pop(pid, None)
                self._totals.pop(pid, None)

    def rates(self, text: str, now: float) -> dict[int, tuple[float, float]]:
        # An ss record without counters is a gap, not a reset to zero.
        current = parse_ss_sockets(text, omit_unreported=True)
        prev = self._prev
        prev_at = self._prev_at
        if prev is None or prev_at is None:
            self._prev = {key: (received, acked, now) for key, (received, acked) in current.items()}
            self._prev_at = now
            for pid, _socket in current:
                self._totals.setdefault(pid, (0, 0))
                self._seen_pids[pid] = now
            return {}
        dt = now - prev_at
        if dt <= 0:
            return {}
        self.primed = True
        self._prev_at = now
        self._prev = {key: value for key, value in prev.items() if now - value[2] <= 60}
        rates: dict[int, list[float]] = {}
        for (pid, socket), (received, acked) in current.items():
            old = prev.get((pid, socket))
            if old is not None and old[2] < prev_at and now - old[2] > 60:
                old = None
            elapsed = now - old[2] if old is not None else dt
            down = max(0, received - old[0]) if old is not None else received
            up = max(0, acked - old[1]) if old is not None else acked
            total_down, total_up = self._totals.get(pid, (0, 0))
            self._totals[pid] = (total_down + down, total_up + up)
            self._seen_pids[pid] = now
            slot = rates.setdefault(pid, [0.0, 0.0])
            slot[0] += down * 8 / elapsed
            slot[1] += up * 8 / elapsed
            self._prev[(pid, socket)] = (received, acked, now)
        return {pid: (pair[0], pair[1]) for pid, pair in rates.items()}


def read_ss() -> str | None:
    """Stdout of ``ss -tinpeH``, or None when the command fails."""
    try:
        result = subprocess.run(
            ["ss", "-tinpeH"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _field(line: str, name: str) -> int | None:
    match = _FIELD_RE[name].search(line)
    if match is None:
        return None
    return int(match.group(1))


def _socket_pids(line: str) -> list[int]:
    if "pid=" not in line:
        return []
    local, peer = _endpoints(line)
    if _is_loopback(local) or _is_loopback(peer):
        return []
    found: list[int] = []
    for match in _PID_RE.finditer(line):
        pid = int(match.group(1))
        if pid not in found:
            found.append(pid)
    return found


def _endpoints(line: str) -> tuple[str, str]:
    head = line.split(" users:", 1)[0]
    ends = [part for part in head.split() if ":" in part]
    if len(ends) >= 2:
        return ends[-2], ends[-1]
    return "", ""


def _is_loopback(endpoint: str) -> bool:
    host = _host(endpoint)
    if not host or host in {"*", "0.0.0.0", "::"}:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host.startswith("127.")
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return addr.is_loopback


def _host(endpoint: str) -> str:
    if endpoint.startswith("["):
        end = endpoint.find("]")
        host = endpoint[1:end] if end > 1 else ""
    else:
        host = endpoint.rsplit(":", 1)[0]
    return host.split("%", 1)[0]
