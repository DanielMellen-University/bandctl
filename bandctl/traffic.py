"""Per-process TCP rates from ``ss``.

``ss -tinp`` prints ``bytes_received`` and ``bytes_acked`` for each socket.
The difference over a second is the rate. Loopback sockets are skipped, so
local services do not look like link traffic. The first sample has no
previous total, so it returns no rates.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess

_PID_RE = re.compile(r"pid=(\d+)")
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


class TrafficSampler:
    """Byte totals in, bit/s out. The first call only stores a baseline."""

    def __init__(self) -> None:
        self._prev: dict[int, tuple[int, int]] | None = None
        self._prev_at: float | None = None
        self.primed = False

    def rates(self, text: str, now: float) -> dict[int, tuple[float, float]]:
        current = parse_ss(text)
        prev = self._prev
        prev_at = self._prev_at
        self._prev = current
        self._prev_at = now
        if prev is None or prev_at is None:
            return {}
        dt = now - prev_at
        self.primed = True
        if dt <= 0:
            return {}
        found: dict[int, tuple[float, float]] = {}
        for pid, (received, acked) in current.items():
            old = prev.get(pid)
            if old is None:
                continue
            down = received - old[0]
            up = acked - old[1]
            if down < 0:
                down = 0
            if up < 0:
                up = 0
            found[pid] = (down * 8 / dt, up * 8 / dt)
        return found


def read_ss() -> str | None:
    """Stdout of ``ss -tinpH``, or None when ``ss`` cannot be run."""
    try:
        result = subprocess.run(
            ["ss", "-tinpH"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 and not result.stdout:
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
    if host.startswith("::ffff:"):
        host = host[7:]
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
