"""Apply percent ceilings with cgroup v2, nftables marks, and HTB.

Upload packets are marked in the nft output hook, which runs before the
device qdisc, so the HTB fw filter on the real interface sees the mark.

Download packets hit the ingress qdisc before netfilter, so that mark is
not on the skb yet. The output rule also sets the conntrack mark. On
ingress, the ctinfo action copies it onto the skb and redirects the packet
through ``ifb-bandctl``, where a second HTB class caps it.

nft's ``socket cgroupv2 level 2`` matches only the second path component.
Leaf cgroups are ``/sys/fs/cgroup/bandctl/bandctl-<pid>``, so the leaf name
is unique. That match sees the cgroup the socket was created in. Moving
the process does not move sockets that already exist, so each open socket
also gets a flower filter from ``/proc/net`` and ``/proc/<pid>/fd``.

Nothing is installed until the first successful apply. ``teardown`` and
``recover`` remove only objects this tool creates. A state file under
``/run`` remembers the previous root qdisc and the original cgroups so a
later start can finish a cleanup that a crash skipped.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from bandctl.procscan import SockFlow, parse_proc_net, parse_start_ticks
from bandctl.rates import htb_plan, percent_to_bps

IFB = "ifb-bandctl"
TABLE = "bandctl"
CGROUP_PARENT = "/sys/fs/cgroup/bandctl"
SAFE_QDISCS = {"fq_codel", "pfifo_fast", "mq", "fq", "pfifo", "cake", "noqueue"}


class Host(Protocol):
    def run(self, args: list[str], input_text: str | None = None) -> str: ...

    def read(self, path: str) -> str: ...

    def write(self, path: str, data: str) -> None: ...

    def mkdir(self, path: str) -> None: ...

    def rmdir(self, path: str) -> None: ...

    def listdir(self, path: str) -> list[str]: ...

    def readlink(self, path: str) -> str: ...

    def unlink(self, path: str) -> None: ...


class ShaperError(RuntimeError):
    """A tc, nft, or cgroup operation failed."""

    def __init__(self, detail: str, command: list[str] | None = None):
        self.detail = detail.strip() or "command failed"
        self.command = list(command or [])
        shown = " ".join(self.command[:6])
        if len(self.command) > 6:
            shown += " ..."
        message = self.detail if not shown else f"{self.detail} [{shown}]"
        super().__init__(message)


@dataclass
class Share:
    pid: int
    down_pct: int | None
    up_pct: int | None
    minor: int
    original_cgroup: str
    command: str = ""
    start_ticks: int | None = None
    revision: int = 0


@dataclass
class InstalledFlow:
    """One flower filter currently installed. The qdisc delete drops it."""

    handle: int
    minor: int
    dev: str
    parent: str
    protocol: str
    prio: int


# filename, proto, family. tcp is parsed before tcp6 so an inode is claimed once.
_NET_TABLES = (
    ("tcp", "tcp", "ip"),
    ("tcp6", "tcp", "ipv6"),
    ("udp", "udp", "ip"),
    ("udp6", "udp", "ipv6"),
)


@dataclass
class Shaper:
    """One interface. Methods take a lock so the UI thread and the sampler can share it."""

    iface: str
    host: Host | None = None
    state_path: str = "/run/bandctl.json"
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _shares: dict[int, Share] = field(default_factory=dict)
    _next_minor: int = 10
    _revision: int = 0
    _down_cap: int | None = None
    _up_cap: int | None = None
    _egress: bool = False
    _ifb: bool = False
    _saved_qdisc: str | None = None
    _classes: set[tuple[str, int]] = field(default_factory=set)
    _filters: set[tuple[str, int, str]] = field(default_factory=set)
    _bytes: dict[tuple[str, int], int] = field(default_factory=dict)
    _sample_at: float = 0.0
    _torn: bool = False
    _flows: dict[tuple, InstalledFlow] = field(default_factory=dict)
    _next_flow_handle: int = 1
    flow_warning: str | None = None

    def __post_init__(self) -> None:
        if self.host is None:
            self.host = LinuxHost()

    @property
    def shares(self) -> dict[int, Share]:
        return dict(self._shares)

    @property
    def download_active(self) -> bool:
        return self._ifb

    @property
    def upload_active(self) -> bool:
        return self._egress

    def apply(
        self,
        pid: int,
        down_pct: int | None,
        up_pct: int | None,
        down_cap: int | None,
        up_cap: int | None,
        command: str = "",
        *,
        expected_start_ticks: int | None = None,
        only_if_unlimited: bool = False,
    ) -> str:
        if down_pct is None and up_pct is None:
            raise ShaperError("enter a download percent, an upload percent, or both")
        if self._torn:
            raise ShaperError("shaper is shut down")
        with self._lock:
            if only_if_unlimited and pid in self._shares:
                raise ShaperError(f"pid {pid} already has a limit")
            if expected_start_ticks is not None:
                try:
                    stat = self.host.read(f"/proc/{pid}/stat")
                except OSError as exc:
                    raise ShaperError(f"cannot verify pid {pid}") from exc
                if parse_start_ticks(stat) != expected_start_ticks:
                    raise ShaperError(f"pid {pid} changed since the process scan")
            if pid not in self._shares:
                self._check_leaf_empty(pid)
            old_shares = {pid_: _copy_share(share) for pid_, share in self._shares.items()}
            old_caps = (self._down_cap, self._up_cap)
            self._revision += 1
            self._down_cap = down_cap
            self._up_cap = up_cap
            created = pid not in self._shares
            if created:
                minor = self._alloc_minor()
                original = self._read_cgroup(pid)
                share = Share(pid, down_pct, up_pct, minor, original, command, expected_start_ticks, self._revision)
            else:
                share = self._shares[pid]
                share.down_pct = down_pct
                share.up_pct = up_pct
                share.command = command or share.command
                share.revision = self._revision
            try:
                self._require_caps()
                if created:
                    self._move_to_leaf(share)
                    self._shares[pid] = share
                self._sync()
            except Exception:
                self._shares = old_shares
                self._down_cap, self._up_cap = old_caps
                if created:
                    try:
                        self._restore_pid(pid, share.original_cgroup)
                    except ShaperError:
                        pass
                    else:
                        # The leaf was made for this apply. Do not leave it behind.
                        self._rmdir_leaf(pid)
                try:
                    self._sync()
                except ShaperError:
                    pass
                raise
        return _applied_text(share)

    def apply_tracked(
        self, pid: int, down_pct: int | None, up_pct: int | None,
        down_cap: int | None, up_cap: int | None, command: str = "", *,
        expected_start_ticks: int | None = None, only_if_unlimited: bool = False,
    ) -> tuple[str, int]:
        """Apply and return the installed revision while holding the lock."""
        with self._lock:
            message = self.apply(pid, down_pct, up_pct, down_cap, up_cap, command,
                                 expected_start_ticks=expected_start_ticks, only_if_unlimited=only_if_unlimited)
            return message, self._shares[pid].revision

    def remove(self, pid: int, *, expected_revision: int | None = None) -> str:
        with self._lock:
            share = self._shares.get(pid)
            if share is None:
                raise ShaperError(f"pid {pid} has no limit")
            if expected_revision is not None and share.revision != expected_revision:
                raise ShaperError(f"limit for pid {pid} changed")
            if self._torn:
                raise ShaperError("shaper is shut down")
            restored = False
            try:
                restored = self._restore_share(share)
            except ShaperError:
                # A pid that has exited has nothing left to move back. A live
                # one stays tracked so the share is not lost.
                if self._pid_alive(pid):
                    raise
            self._shares.pop(pid)
            try:
                self._sync()
            except ShaperError:
                self._shares[pid] = share
                if restored:
                    try:
                        self._move_to_leaf(share)
                    except ShaperError:
                        pass
                raise
            self._rmdir_leaf(pid)
        return f"removed limits on pid {pid}"

    def set_capacities(self, down_cap: int | None, up_cap: int | None) -> None:
        """Recompute class rates after the link speed or a budget changes."""
        with self._lock:
            if self._torn or not self._shares:
                self._down_cap = down_cap
                self._up_cap = up_cap
                return
            old = (self._down_cap, self._up_cap)
            self._down_cap = down_cap
            self._up_cap = up_cap
            try:
                self._require_caps()
                self._sync()
            except Exception:
                self._down_cap, self._up_cap = old
                raise

    def sample(self) -> dict[int, tuple[float, float]]:
        """pid -> (download bit/s, upload bit/s) since the previous sample."""
        with self._lock:
            if self._torn:
                return {}
            try:
                self._retire_exited()
                self._sync_flows()
            except ShaperError as exc:
                warning = str(exc)
                if warning != self.flow_warning:
                    self.flow_warning = warning
            else:
                self.flow_warning = None
            now = time.monotonic()
            dt = now - self._sample_at if self._sample_at else 0.0
            down = self._class_bytes(IFB) if self._ifb else {}
            up = self._class_bytes(self.iface) if self._egress else {}
            result: dict[int, tuple[float, float]] = {}
            by_minor: dict[int, tuple[float, float]] = {}
            for pid, share in self._shares.items():
                if share.minor not in by_minor:
                    by_minor[share.minor] = (
                        self._delta_bps("down", share.minor, down.get(share.minor), dt),
                        self._delta_bps("up", share.minor, up.get(share.minor), dt),
                    )
                result[pid] = by_minor[share.minor]
            self._sample_at = now
            return result

    def _process_status(self, share: Share) -> str:
        if share.start_ticks is None:
            return "unknown"
        try:
            stat = self.host.read(f"/proc/{share.pid}/stat")
        except FileNotFoundError:
            return "gone"
        except OSError:
            return "unknown"
        start = parse_start_ticks(stat)
        if start is None:
            return "unknown"
        return "same" if start == share.start_ticks else "changed"

    def _retire_exited(self) -> None:
        stale = [share for share in self._shares.values() if self._process_status(share) in ("gone", "changed")]
        if not stale:
            return
        for share in stale:
            self._shares.pop(share.pid)
        try:
            self._sync()
        except ShaperError:
            self._shares.update({share.pid: share for share in stale})
            raise
        for share in stale:
            self._rmdir_leaf(share.pid)

    def _restore_share(self, share: Share) -> bool:
        status = self._process_status(share)
        if status in ("gone", "changed"):
            return False
        if share.start_ticks is not None and status == "unknown":
            raise ShaperError(f"cannot verify pid {share.pid}")
        self._restore_pid(share.pid, share.original_cgroup)
        return True

    def recover(self) -> list[str]:
        """Best-effort cleanup of a previous run. Never raises."""
        notes: list[str] = []
        with self._lock:
            state = self._read_state()
            if state and state.get("iface"):
                iface = str(state["iface"])
                qdisc = str(state.get("qdisc") or "fq_codel")
                for pid_text, original in (state.get("pids") or {}).items():
                    try:
                        self._restore_pid(int(pid_text), str(original))
                        notes.append(f"restored pid {pid_text}")
                    except ShaperError as exc:
                        notes.append(str(exc))
                self._saved_qdisc = qdisc
                notes.extend(self._scrub(iface, restore_qdisc=True))
            else:
                notes.extend(self._scrub(self.iface, restore_qdisc=False))
            self._shares.clear()
            self._egress = False
            self._ifb = False
            self._classes.clear()
            self._filters.clear()
            self._unlink_state()
        return notes

    def teardown(self) -> list[str]:
        """Idempotent. Puts processes back and deletes qdiscs, nft, and cgroups."""
        notes: list[str] = []
        with self._lock:
            if self._torn:
                return notes
            self._torn = True
            for share in list(self._shares.values()):
                try:
                    self._restore_share(share)
                except ShaperError as exc:
                    notes.append(str(exc))
            self._shares.clear()
            # Only put a root qdisc back if this run replaced it. Otherwise a
            # failed cleanup would swap out a qdisc bandctl never touched.
            replaced_root = self._egress
            try:
                self._sync()
            except ShaperError as exc:
                notes.append(str(exc))
                notes.extend(self._scrub(self.iface, restore_qdisc=replaced_root))
            self._remove_tree()
            self._unlink_state()
        return notes

    # --- internals -------------------------------------------------------

    def _alloc_minor(self) -> int:
        used = {share.minor for share in self._shares.values()}
        minor = self._next_minor
        while minor in used or minor == 999:
            minor += 1
        self._next_minor = minor + 1
        return minor

    def _require_caps(self) -> None:
        for share in self._shares.values():
            if share.down_pct is not None and not self._down_cap:
                raise ShaperError("download capacity is unknown; enter a download budget")
            if share.up_pct is not None and not self._up_cap:
                raise ShaperError("upload capacity is unknown; enter an upload budget")

    def _read_cgroup(self, pid: int) -> str:
        try:
            text = self.host.read(f"/proc/{pid}/cgroup")
        except OSError as exc:
            raise ShaperError(f"pid {pid} is not running") from exc
        for line in text.splitlines():
            if "::" in line:
                path = line.split("::", 1)[1].strip() or "/"
                return path
        raise ShaperError(f"pid {pid} has no cgroup v2 path")

    def _move_to_leaf(self, share: Share) -> None:
        parent = CGROUP_PARENT
        leaf = f"{parent}/bandctl-{share.pid}"
        self.host.mkdir(parent)
        self.host.mkdir(leaf)
        self.host.write(f"{leaf}/cgroup.procs", f"{share.pid}\n")

    def _check_leaf_empty(self, pid: int) -> None:
        try:
            members = self.host.read(f"{CGROUP_PARENT}/bandctl-{pid}/cgroup.procs")
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ShaperError(f"cannot inspect cgroup for pid {pid}") from exc
        if members.strip():
            raise ShaperError(f"cgroup for pid {pid} still contains processes")

    def _restore_pid(self, pid: int, original: str) -> None:
        if original == "/":
            dest = "/sys/fs/cgroup/cgroup.procs"
        else:
            dest = "/sys/fs/cgroup" + original.rstrip("/") + "/cgroup.procs"
        try:
            self.host.write(dest, f"{pid}\n")
        except (OSError, ShaperError) as exc:
            raise ShaperError(f"could not restore pid {pid}: {exc}") from exc

    def _pid_alive(self, pid: int) -> bool:
        try:
            self.host.read(f"/proc/{pid}/cgroup")
        except OSError:
            return False
        return True

    def _rmdir_leaf(self, pid: int) -> None:
        leaf = f"{CGROUP_PARENT}/bandctl-{pid}"
        try:
            self.host.rmdir(leaf)
        except OSError:
            return
        try:
            if not self.host.listdir(CGROUP_PARENT):
                self.host.rmdir(CGROUP_PARENT)
        except OSError:
            return

    def _remove_tree(self) -> None:
        try:
            names = self.host.listdir(CGROUP_PARENT)
        except OSError:
            return
        for name in names:
            try:
                procs = self.host.read(f"{CGROUP_PARENT}/{name}/cgroup.procs")
            except OSError:
                procs = ""
            for pid_text in procs.split():
                try:
                    self._restore_pid(int(pid_text), "/")
                except (ShaperError, ValueError):
                    pass
            try:
                self.host.rmdir(f"{CGROUP_PARENT}/{name}")
            except OSError:
                pass
        try:
            self.host.rmdir(CGROUP_PARENT)
        except OSError:
            pass

    def _sync(self) -> None:
        down_shares = [share for share in self._shares.values() if share.down_pct]
        up_shares = [share for share in self._shares.values() if share.up_pct]
        if up_shares:
            self._ensure_egress()
        elif self._egress:
            self._restore_root_qdisc()
        if down_shares:
            self._ensure_ifb()
        elif self._ifb:
            self._destroy_ifb()
        if self._shares:
            self._run(["nft", "-f", "-"], nft_script(list(self._shares.values())))
        else:
            self._delete_nft(strict=True)
        if self._egress and self._up_cap:
            self._sync_direction(self.iface, up_shares, self._up_cap)
        if self._ifb and self._down_cap:
            self._sync_direction(IFB, down_shares, self._down_cap)
        self._sync_flows()
        self._write_state()

    def _ensure_egress(self) -> None:
        if self._egress:
            return
        current = self._root_kind(self.iface)
        if current == "htb" and self._saved_qdisc is None:
            raise ShaperError(
                f"{self.iface} already has an htb qdisc; bandctl will not replace it"
            )
        if current not in SAFE_QDISCS and current != "htb":
            raise ShaperError(
                f"{self.iface} has root qdisc {current}; bandctl will not replace it"
            )
        if self._saved_qdisc is None:
            self._saved_qdisc = current if current in SAFE_QDISCS else "fq_codel"
        self._run(
            ["tc", "qdisc", "replace", "dev", self.iface, "root", "handle", "1:", "htb", "default", "999"]
        )
        self._egress = True

    def _ensure_ifb(self) -> None:
        if self._ifb:
            return
        try:
            self._run(["modprobe", "ifb"])
            self._run(["modprobe", "sch_ingress"])
            self._run(["modprobe", "cls_matchall"])
            self._run(["modprobe", "act_ctinfo"])
            self._run(["modprobe", "act_mirred"])
            try:
                self._run(["ip", "link", "add", IFB, "type", "ifb"])
            except ShaperError as exc:
                if "File exists" not in str(exc):
                    raise
            self._run(["ip", "link", "set", IFB, "up"])
            try:
                self._run(["tc", "qdisc", "add", "dev", self.iface, "handle", "ffff:", "ingress"])
            except ShaperError as exc:
                if "exists" not in str(exc).lower():
                    raise
            # u32 rejects protocol all, so a catch-all has to be matchall.
            # prio 10 leaves room for a more specific filter to run first.
            self._run(
                [
                    "tc", "filter", "add", "dev", self.iface, "parent", "ffff:",
                    "prio", "10", "matchall",
                    "action", "ctinfo", "cpmark", "pipe",
                    "action", "mirred", "egress", "redirect", "dev", IFB,
                ]
            )
            self._run(
                ["tc", "qdisc", "replace", "dev", IFB, "root", "handle", "1:", "htb", "default", "999"]
            )
        except ShaperError:
            self._destroy_ifb()
            raise
        self._ifb = True

    def _sync_direction(self, dev: str, shares: list[Share], capacity: int) -> None:
        # Members with the same minor share one class and one ceiling.
        unique: dict[int, Share] = {}
        for share in shares:
            previous = unique.get(share.minor)
            if previous is not None and (previous.down_pct, previous.up_pct) != (share.down_pct, share.up_pct):
                raise ShaperError(f"conflicting limits for class {share.minor}")
            unique[share.minor] = share
        shares = list(unique.values())
        ceils_bps: list[int] = []
        for share in shares:
            percent = share.down_pct if dev == IFB else share.up_pct
            if percent is None:
                raise ShaperError(f"pid {share.pid} is missing a percent for {dev}")
            ceils_bps.append(percent_to_bps(percent, capacity))
        rates, ceils, default = htb_plan(ceils_bps, capacity)
        parent = sum(rates) + default
        self._class(dev, "1:", "1:1", parent, parent)
        self._class(dev, "1:1", "1:999", default, parent)
        desired = {share.minor for share in shares}
        for (have_dev, minor) in list(self._classes):
            if have_dev == dev and minor not in desired and minor not in (1, 999):
                self._delete_class(dev, minor)
        for share, rate, ceil in zip(shares, rates, ceils):
            self._class(dev, "1:1", f"1:{share.minor}", rate, ceil)
            self._filter(dev, share.minor)

    def _class(self, dev: str, parent: str, classid: str, rate: int, ceil: int) -> None:
        if rate < 1:
            rate = 1
        if ceil < rate:
            ceil = rate
        self._run(
            [
                "tc", "class", "replace", "dev", dev, "parent", parent, "classid", classid,
                "htb", "rate", f"{rate}kbit", "ceil", f"{ceil}kbit",
            ]
        )
        minor = int(classid.split(":")[1])
        if minor not in (1, 999):
            self._classes.add((dev, minor))

    def _filter(self, dev: str, minor: int) -> None:
        for proto in ("ip", "ipv6"):
            key = (dev, minor, proto)
            if key in self._filters:
                continue
            self._run(
                [
                    "tc", "filter", "add", "dev", dev, "parent", "1:",
                    "protocol", proto, "prio", "1", "handle", str(minor),
                    "fw", "flowid", f"1:{minor}",
                ]
            )
            self._filters.add(key)

    def _delete_class(self, dev: str, minor: int) -> None:
        for proto in ("ip", "ipv6"):
            try:
                self._run(
                    [
                        "tc", "filter", "del", "dev", dev, "parent", "1:",
                        "protocol", proto, "prio", "1", "handle", str(minor), "fw",
                    ]
                )
            except ShaperError:
                pass
            self._filters.discard((dev, minor, proto))
        try:
            self._run(["tc", "class", "del", "dev", dev, "classid", f"1:{minor}"])
        except ShaperError:
            pass
        self._classes.discard((dev, minor))

    def _restore_root_qdisc(self) -> None:
        kind = self._saved_qdisc or "fq_codel"
        try:
            self._run(["tc", "qdisc", "del", "dev", self.iface, "root"])
        except ShaperError:
            pass
        try:
            self._run(["tc", "qdisc", "add", "dev", self.iface, "root", kind])
        except ShaperError:
            pass
        self._egress = False
        self._drop_dev(self.iface)
        # The root qdisc took the upload flower filters with it.
        self._forget_flows(self.iface, "1:")

    def _destroy_ifb(self) -> None:
        try:
            self._run(["tc", "qdisc", "del", "dev", self.iface, "ingress"])
        except ShaperError:
            pass
        try:
            self._run(["tc", "qdisc", "del", "dev", IFB, "root"])
        except ShaperError:
            pass
        try:
            self._run(["ip", "link", "del", IFB])
        except ShaperError:
            pass
        self._ifb = False
        self._drop_dev(IFB)
        # Ingress lived on the real device. Deleting it drops download flowers.
        self._forget_flows(self.iface, "ffff:")

    def _drop_dev(self, dev: str) -> None:
        self._classes = {item for item in self._classes if item[0] != dev}
        self._filters = {item for item in self._filters if item[0] != dev}

    def _delete_nft(self, *, strict: bool) -> None:
        try:
            self._run(["nft", "delete", "table", "inet", TABLE])
        except ShaperError as exc:
            text = str(exc).lower()
            if "no such file" in text or "does not exist" in text or "not found" in text:
                return
            if strict:
                raise

    def _scrub(self, iface: str, *, restore_qdisc: bool) -> list[str]:
        notes: list[str] = []
        self._delete_nft(strict=False)
        try:
            self._run(["ip", "link", "show", IFB])
            self._destroy_ifb()
            notes.append(f"removed {IFB}")
        except ShaperError:
            pass
        if restore_qdisc:
            self._restore_root_qdisc()
            notes.append(f"restored root qdisc on {iface}")
        self._remove_tree()
        return notes

    def _root_kind(self, iface: str) -> str:
        try:
            text = self._run(["tc", "qdisc", "show", "dev", iface])
        except ShaperError as exc:
            raise ShaperError(f"cannot read qdisc on {iface}: {exc.detail}", exc.command) from exc
        kind = parse_root_qdisc(text)
        if not kind:
            raise ShaperError(f"no root qdisc on {iface}")
        return kind

    def _class_bytes(self, dev: str) -> dict[int, int]:
        try:
            text = self._run(["tc", "-s", "class", "show", "dev", dev])
        except ShaperError:
            return {}
        return parse_class_bytes(text)

    def _delta_bps(self, kind: str, minor: int, total: int | None, dt: float) -> float:
        key = (kind, minor)
        if total is None:
            return 0.0
        previous = self._bytes.get(key)
        self._bytes[key] = total
        if previous is None or dt <= 0 or total < previous:
            return 0.0
        return (total - previous) * 8 / dt

    def _run(self, args: list[str], input_text: str | None = None) -> str:
        try:
            return self.host.run(args, input_text)
        except ShaperError:
            raise
        except OSError as exc:
            raise ShaperError(str(exc), args) from exc

    def _write_state(self) -> None:
        if not self._shares and not self._egress and not self._ifb:
            self._unlink_state()
            return
        payload = {
            "iface": self.iface,
            "qdisc": self._saved_qdisc or "fq_codel",
            "pids": {str(share.pid): share.original_cgroup for share in self._shares.values()},
        }
        try:
            self.host.write(self.state_path, json.dumps(payload) + "\n")
        except OSError:
            return

    def _read_state(self) -> dict | None:
        try:
            raw = self.host.read(self.state_path)
        except OSError:
            return None
        if not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _unlink_state(self) -> None:
        try:
            self.host.unlink(self.state_path)
        except OSError:
            return

    def _sync_flows(self) -> None:
        """Install or remove flower filters so open sockets follow the shares.

        nft only matches sockets created after the cgroup move. This covers
        the ones that already existed, and drops filters when they close.
        """
        desired: dict[tuple, tuple[SockFlow, int, str]] = {}
        net = self._load_net() if self._shares else {}
        for pid, share in self._shares.items():
            for flow in self._pid_flows(pid, net):
                ident = _flow_id(flow)
                if share.up_pct and self._egress:
                    desired[("up", ident)] = (flow, share.minor, "up")
                if share.down_pct and self._ifb:
                    desired[("down", ident)] = (flow, share.minor, "down")
        for key in list(self._flows):
            wanted = desired.get(key)
            if wanted is None or wanted[1] != self._flows[key].minor:
                self._delete_flow(key)
        pending = [key for key in desired if key not in self._flows]
        if not pending:
            return
        self._run(["modprobe", "cls_flower"])
        if any(desired[key][2] == "down" for key in pending):
            self._run(["modprobe", "act_skbedit"])
        for key in pending:
            flow, minor, direction = desired[key]
            self._add_flow(key, flow, minor, direction)

    def _load_net(self) -> dict[str, SockFlow]:
        table: dict[str, SockFlow] = {}
        assert self.host is not None
        for filename, proto, family in _NET_TABLES:
            try:
                text = self.host.read(f"/proc/net/{filename}")
            except OSError:
                continue
            for inode, flow in parse_proc_net(text, proto, family).items():
                table.setdefault(inode, flow)
        return table

    def _pid_flows(self, pid: int, net: dict[str, SockFlow]) -> list[SockFlow]:
        assert self.host is not None
        fd_dir = f"/proc/{pid}/fd"
        try:
            names = self.host.listdir(fd_dir)
        except OSError:
            return []
        found: list[SockFlow] = []
        seen: set[str] = set()
        for name in names:
            try:
                target = self.host.readlink(f"{fd_dir}/{name}")
            except OSError:
                continue
            if not (target.startswith("socket:[") and target.endswith("]")):
                continue
            inode = target[len("socket:[") : -1]
            flow = net.get(inode)
            if flow is None or inode in seen:
                continue
            seen.add(inode)
            found.append(flow)
        return found

    def _add_flow(self, key: tuple, flow: SockFlow, minor: int, direction: str) -> None:
        if direction == "up":
            dev, parent, prio = self.iface, "1:", 2
        else:
            dev, parent, prio = self.iface, "ffff:", 1
        handle = self._next_flow_handle
        self._run(
            flower_command(
                dev=dev,
                parent=parent,
                prio=prio,
                handle=handle,
                flow=flow,
                minor=minor,
                direction=direction,
            )
        )
        self._next_flow_handle = handle + 1
        self._flows[key] = InstalledFlow(handle, minor, dev, parent, flow.family, prio)

    def _delete_flow(self, key: tuple) -> None:
        rec = self._flows.get(key)
        if rec is None:
            return
        try:
            self._run(
                [
                    "tc", "filter", "del", "dev", rec.dev, "parent", rec.parent,
                    "protocol", rec.protocol, "prio", str(rec.prio),
                    "handle", str(rec.handle), "flower",
                ]
            )
        except ShaperError:
            pass
        self._flows.pop(key, None)

    def _forget_flows(self, dev: str, parent: str) -> None:
        self._flows = {
            key: rec
            for key, rec in self._flows.items()
            if not (rec.dev == dev and rec.parent == parent)
        }


def _copy_share(share: Share) -> Share:
    return Share(share.pid, share.down_pct, share.up_pct, share.minor, share.original_cgroup, share.command,
                 share.start_ticks, share.revision)


def _applied_text(share: Share) -> str:
    down = "unlimited" if share.down_pct is None else f"{share.down_pct}%"
    up = "unlimited" if share.up_pct is None else f"{share.up_pct}%"
    return f"pid {share.pid} download {down}, upload {up}"


def _flow_id(flow: SockFlow) -> tuple:
    return (
        flow.proto,
        flow.family,
        flow.local_ip,
        flow.local_port,
        flow.remote_ip,
        flow.remote_port,
    )


def flower_command(
    *,
    dev: str,
    parent: str,
    prio: int,
    handle: int,
    flow: SockFlow,
    minor: int,
    direction: str,
) -> list[str]:
    """One ``tc filter add`` for a socket.

    Upload matches the local end as the source and lands in the HTB class.
    Download matches the local end as the destination, stamps the mark, and
    redirects onto the IFB device. A remote port of 0 (unconnected UDP) is
    left out so every packet from that local port is included.
    """
    command = [
        "tc", "filter", "add", "dev", dev, "parent", parent,
        "protocol", flow.family, "prio", str(prio), "handle", str(handle),
        "flower", "ip_proto", flow.proto,
    ]
    if direction == "up":
        command += ["src_ip", flow.local_ip, "src_port", str(flow.local_port)]
        if flow.remote_port:
            command += ["dst_ip", flow.remote_ip, "dst_port", str(flow.remote_port)]
        command += ["classid", f"1:{minor}"]
        return command
    command += ["dst_ip", flow.local_ip, "dst_port", str(flow.local_port)]
    if flow.remote_port:
        command += ["src_ip", flow.remote_ip, "src_port", str(flow.remote_port)]
    command += [
        "action", "skbedit", "mark", str(minor), "pipe",
        "action", "mirred", "egress", "redirect", "dev", IFB,
    ]
    return command


def leaf_name(pid: int) -> str:
    return f"bandctl-{pid}"


def nft_script(shares: list[Share]) -> str:
    """Full replace of table ``inet bandctl``. Level 2 is the leaf directory name."""
    lines = [
        "add table inet bandctl",
        "flush table inet bandctl",
        "add chain inet bandctl output { type filter hook output priority mangle; policy accept; }",
        "add chain inet bandctl prerouting { type filter hook prerouting priority mangle; policy accept; }",
    ]
    for share in shares:
        name = leaf_name(share.pid)
        mark = share.minor
        match = f'socket cgroupv2 level 2 "{name}"'
        lines.append(
            f"add rule inet bandctl output {match} ct mark set {mark} meta mark set {mark}"
        )
        lines.append(
            f"add rule inet bandctl prerouting {match} ct mark set {mark} meta mark set {mark}"
        )
    return "\n".join(lines) + "\n"


def parse_root_qdisc(text: str) -> str | None:
    for line in text.splitlines():
        match = re.search(r"^qdisc\s+(\S+)\s+\S+\s+root\b", line)
        if match:
            return match.group(1)
    return None


def parse_class_bytes(text: str) -> dict[int, int]:
    """Minor class id -> bytes sent, from ``tc -s class show``."""
    found: dict[int, int] = {}
    chunks = re.split(r"(?=^class htb )", text, flags=re.MULTILINE)
    for chunk in chunks:
        header = re.match(r"class htb 1:(\d+)", chunk)
        sent = re.search(r"Sent (\d+) bytes", chunk)
        if not header or not sent:
            continue
        minor = int(header.group(1))
        if minor in (1, 999):
            continue
        found[minor] = int(sent.group(1))
    return found


class LinuxHost:
    """Real processes and the real sysfs. Used only while bandctl is root."""

    def run(self, args: list[str], input_text: str | None = None) -> str:
        try:
            proc = subprocess.run(
                args,
                input=input_text,
                text=True,
                capture_output=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ShaperError(f"command not found: {args[0]}", args) from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
            raise ShaperError(detail, args)
        return proc.stdout

    def read(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8", errors="replace")

    def readlink(self, path: str) -> str:
        return os.readlink(path)

    def write(self, path: str, data: str) -> None:
        try:
            Path(path).write_text(data, encoding="utf-8")
        except OSError as exc:
            raise ShaperError(f"{path}: {exc.strerror or exc}") from exc

    def mkdir(self, path: str) -> None:
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ShaperError(f"{path}: {exc.strerror or exc}") from exc

    def rmdir(self, path: str) -> None:
        Path(path).rmdir()

    def listdir(self, path: str) -> list[str]:
        return sorted(entry.name for entry in Path(path).iterdir())

    def unlink(self, path: str) -> None:
        Path(path).unlink(missing_ok=True)
