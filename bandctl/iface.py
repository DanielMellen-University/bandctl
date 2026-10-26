"""Default-route device and its reported link speed."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


class IfaceError(RuntimeError):
    """No default route, or the interface name is not a real device."""


def parse_ip_route(text: str) -> str | None:
    """First ``dev`` on a ``default`` line from ``ip route show default``."""
    for line in text.splitlines():
        if not line.startswith("default "):
            continue
        match = re.search(r"\bdev\s+(\S+)", line)
        if match:
            return match.group(1)
    return None


def parse_proc_route(text: str) -> str | None:
    """Interface whose destination is ``00000000`` in ``/proc/net/route``."""
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "00000000":
            return parts[0]
    return None


def parse_speed_mbps(text: str) -> int | None:
    """Sysfs ``speed`` is megabits per second, or ``-1`` when the driver does not know."""
    try:
        value = int(text.strip())
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


def default_iface(run=None, proc_route: Path | None = None) -> str:
    """Device used by the IPv4 default route.

    ``ip route`` wins. ``/proc/net/route`` is the fallback when ``ip`` is missing.
    """
    if run is None:
        run = _run
    try:
        text = run(["ip", "route", "show", "default"])
    except (OSError, subprocess.CalledProcessError):
        text = ""
    name = parse_ip_route(text or "")
    if name:
        return name
    path = proc_route or Path("/proc/net/route")
    try:
        name = parse_proc_route(path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        raise IfaceError("could not read the routing table") from exc
    if not name:
        raise IfaceError("no default route; pass --iface")
    return name


def list_ifaces(net_root: Path | None = None) -> list[str]:
    """Device names under ``/sys/class/net``, sorted. Empty when it cannot be read."""
    root = net_root or Path("/sys/class/net")
    try:
        return sorted(entry.name for entry in root.iterdir())
    except OSError:
        return []


def check_iface(name: str, net_root: Path | None = None) -> str:
    """Return *name* if it is a real network device, or raise IfaceError.

    Linux names are at most 15 bytes with no slash or whitespace. The name
    must also exist under ``/sys/class/net``.
    """
    root = net_root or Path("/sys/class/net")
    valid = (
        bool(name)
        and len(name.encode("utf-8", errors="replace")) <= 15
        and name not in (".", "..")
        and "/" not in name
        and not any(char.isspace() for char in name)
    )
    if not valid:
        raise IfaceError(f"{name!r} is not a valid interface name")
    if not (root / name).exists():
        known = list_ifaces(root)
        hint = f" Interfaces here: {', '.join(known)}." if known else ""
        raise IfaceError(f"no network interface named {name!r}.{hint}")
    return name


def link_speed_bps(iface: str, speed_path: Path | None = None) -> int | None:
    """Link speed in bit/s, or None when sysfs has no positive speed."""
    path = speed_path or Path(f"/sys/class/net/{iface}/speed")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    mbps = parse_speed_mbps(text)
    if mbps is None:
        return None
    return mbps * 1_000_000


def _run(args: list[str]) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return ""
    return proc.stdout
