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
    """Interface on the lowest-metric active IPv4 default route."""
    best: tuple[int, str] | None = None
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 8 or parts[1] != "00000000" or parts[7] != "00000000":
            continue
        try:
            flags = int(parts[3], 16)
            metric = int(parts[6])
        except ValueError:
            continue
        if flags & 1 and (best is None or metric < best[0]):
            best = (metric, parts[0])
    return best[1] if best else None


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

    ``ip route`` wins. ``/proc/net/route`` is the fallback if it fails or times out.
    """
    if run is None:
        run = _run
    try:
        text = run(["ip", "route", "show", "default"])
    except (OSError, UnicodeDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        text = ""
    name = parse_ip_route(text or "")
    if name:
        return _validate_iface_name(name)
    path = proc_route or Path("/proc/net/route")
    try:
        name = parse_proc_route(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise IfaceError("routing table is not valid UTF-8; pass --iface") from exc
    except OSError as exc:
        raise IfaceError("could not read the routing table") from exc
    if not name:
        raise IfaceError("no default route; pass --iface")
    return _validate_iface_name(name)


def list_ifaces(net_root: Path | None = None) -> list[str]:
    """Device names under ``/sys/class/net``, sorted. Empty when it cannot be read."""
    root = net_root or Path("/sys/class/net")
    try:
        return sorted(entry.name for entry in root.iterdir())
    except OSError:
        return []


def _validate_iface_name(name: str) -> str:
    """Reject names that cannot be saved and restored safely."""
    valid = (
        bool(name)
        and name not in (".", "..")
        and "/" not in name
        and not any(char.isspace() or ord(char) < 32 or ord(char) == 127
                    or 0xD800 <= ord(char) <= 0xDFFF for char in name)
        and len(name.encode("utf-8")) <= 15
    )
    if not valid:
        raise IfaceError(f"{name!r} is not a valid interface name")
    return name


def check_iface(name: str, net_root: Path | None = None) -> str:
    """Return a valid device name that exists under ``/sys/class/net``."""
    _validate_iface_name(name)
    root = net_root or Path("/sys/class/net")
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
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", check=False, timeout=2)
    if proc.returncode != 0:
        return ""
    return proc.stdout
