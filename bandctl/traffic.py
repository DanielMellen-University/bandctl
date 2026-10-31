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
    received = acked = 0
    source = None

    def finish_record() -> None:
        if received == 0 and acked == 0:
            return
        for pid in pids:
            slot = totals.setdefault(pid, [0, 0])
            slot[0] += received
            slot[1] += acked

    for raw in text.split("\n"):
        if not raw.strip():
            continue
        if raw[0].isspace():
            if not pids:
                continue
            down = _field(raw, "bytes_received")
            up = _field(raw, "bytes_acked")
            if down is not None:
                received = down
            if up is not None:
                acked, source = up, "acked"
            elif source != "acked":
                up = _field(raw, "bytes_sent")
                if up is not None:
                    acked, source = up, "sent"
            continue
        finish_record()
        pids = _socket_pids(raw)
        received = acked = 0
        source = None
    finish_record()
    return {pid: (received, acked) for pid, (received, acked) in totals.items()}


def parse_ss_sockets(text: str, *, omit_unreported: bool = False) -> dict[tuple[int, str], tuple[int, int]]:
    """Map owned sockets to counters, optionally omitting records without counters."""
    parsed = _parse_ss_socket_counters(text)
    return {key: (received or 0, acked or 0) for key, (received, acked, _source) in parsed.items()
            if not omit_unreported or received is not None or acked is not None}


def _parse_ss_socket_counters(text: str) -> dict[tuple[int, str], tuple[int | None, int | None, str | None]]:
    found: dict[tuple[int, str], tuple[int | None, int | None, str | None]] = {}
    keys: list[tuple[int, str]] = []
    for raw in text.split("\n"):
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
                found[key] = (None, None, None)
            continue
        received = _field(raw, "bytes_received")
        acked = _field(raw, "bytes_acked")
        source = "acked" if acked is not None else None
        if acked is None:
            acked = _field(raw, "bytes_sent")
            if acked is not None:
                source = "sent"
        if received is None and acked is None:
            continue
        for key in keys:
            previous = found[key]
            use_upload = acked is not None and not (source == "sent" and previous[2] == "acked")
            found[key] = (previous[0] if received is None else received,
                          acked if use_upload else previous[1],
                          source if use_upload else previous[2])
    return found


class TrafficSampler:
    """Track socket counters and return per-process rates and session totals."""

    def __init__(self) -> None:
        # Counters, their sample times, last socket sighting, and upload counter type.
        self._prev: dict[tuple[int, str], tuple[int | None, int | None, float, float, float, str | None]] | None = None
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
        observed = _parse_ss_socket_counters(text)
        current = {key: value for key, value in observed.items()
                   if value[:2] != (None, None)}
        prev = self._prev
        prev_at = self._prev_at
        if prev is None or prev_at is None:
            self._prev = {key: (received, acked, now, now, now, source)
                          for key, (received, acked, source) in observed.items()}
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
        self._prev = {key: value for key, value in prev.items() if now - value[4] <= 60}
        for key, value in observed.items():
            old = prev.get(key)
            if (value[:2] == (None, None) and old is not None
                    and (old[4] >= prev_at or now - old[4] <= 60)):
                self._prev[key] = (*old[:4], now, old[5])
        rates: dict[int, list[float]] = {}
        for (pid, socket), (received, acked, source) in current.items():
            old = prev.get((pid, socket))
            if old is not None and old[4] < prev_at and now - old[4] > 60:
                old = None
            def delta(value: int | None, baseline: int | None) -> int:
                if value is None or baseline is None:
                    return 0
                return max(0, value - baseline)

            # New sockets contribute from zero. Missing initial counters need a baseline.
            down = delta(received, old[0] if old is not None else 0)
            up = delta(acked, old[1] if old is not None else 0)
            # Sent and acknowledged bytes have different baselines.
            if old is not None and source is not None and old[5] is not None and source != old[5]:
                up = 0
            total_down, total_up = self._totals.get(pid, (0, 0))
            self._totals[pid] = (total_down + down, total_up + up)
            self._seen_pids[pid] = now
            slot = rates.setdefault(pid, [0.0, 0.0])
            slot[0] += down * 8 / (now - old[2] if old is not None else dt)
            slot[1] += up * 8 / (now - old[3] if old is not None else dt)
            self._prev[(pid, socket)] = (
                received if received is not None else old[0] if old is not None else 0,
                acked if acked is not None else old[1] if old is not None else 0,
                now if received is not None or old is None else old[2],
                now if acked is not None or old is None else old[3],
                now,
                source if source is not None else old[5] if old is not None else None,
            )
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
