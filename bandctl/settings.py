"""Validated interchange format for explicitly exported settings."""

from __future__ import annotations

import json
from uuid import UUID
from dataclasses import dataclass

from bandctl.groups import Group, active_groups, validate_groups
from bandctl.rules import AutoRule
from bandctl.schedule import DailyWindow, parse_time


@dataclass(frozen=True)
class GroupSettings:
    group: Group
    starts: tuple[int, ...]

    def __post_init__(self) -> None:
        if len(self.starts) != len(self.group.members) or any(type(value) is not int or value < 0 for value in self.starts):
            raise ValueError("group members need valid process start times")


@dataclass(frozen=True)
class Settings:
    iface: str
    down_budget: int | None
    up_budget: int | None
    rules: tuple[AutoRule, ...]
    groups: tuple[GroupSettings, ...]
    boot_id: str | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.iface, str) or not self.iface or self.iface in (".", "..")
                or len(self.iface.encode("utf-8")) > 15 or "/" in self.iface
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in self.iface)):
            raise ValueError("invalid interface name")
        for budget in (self.down_budget, self.up_budget):
            if budget is not None and (type(budget) is not int or budget < 1):
                raise ValueError("budgets must be positive whole numbers of bits per second")
        validate_groups([item.group for item in self.groups])
        if self.groups and self.boot_id is None:
            raise ValueError("groups need a boot ID to verify process identities")
        if self.boot_id is not None:
            try:
                canonical = str(UUID(self.boot_id))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError("invalid boot ID") from exc
            if canonical != self.boot_id:
                raise ValueError("invalid boot ID")


def _object(value, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"invalid {label} fields")
    return value


def _list(value, label: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate field {key}")
        result[key] = value
    return result


def loads(text: str) -> Settings:
    """Parse and validate every setting before returning any of them."""
    try:
        raw = json.loads(text, object_pairs_hook=_unique_pairs)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise ValueError("invalid settings JSON") from exc
    obj = _object(raw, {"version", "interface", "down_budget_bps", "up_budget_bps", "rules", "groups", "boot_id"}, "settings")
    if type(obj["version"]) is not int or obj["version"] != 1:
        raise ValueError("unsupported settings version")
    rules = []
    for value in _list(obj["rules"], "rules"):
        item = _object(value, {"name", "down", "up", "start", "end"}, "rule")
        start, end = item["start"], item["end"]
        if start is None and end is None:
            window = None
        elif isinstance(start, str) and isinstance(end, str):
            window = DailyWindow(parse_time(start), parse_time(end))
        else:
            raise ValueError("rule needs both schedule times or neither")
        if not isinstance(item["name"], str):
            raise ValueError("rule name must be text")
        rules.append(AutoRule(item["name"], item["down"], item["up"], window))
    groups = []
    for value in _list(obj["groups"], "groups"):
        item = _object(value, {"name", "down", "up", "members"}, "group")
        members = [_object(member, {"pid", "start_ticks"}, "group member")
                   for member in _list(item["members"], "group members")]
        group = Group(item["name"], item["down"], item["up"], tuple(member["pid"] for member in members))
        groups.append(GroupSettings(group, tuple(member["start_ticks"] for member in members)))
    return Settings(obj["interface"], obj["down_budget_bps"], obj["up_budget_bps"], tuple(rules), tuple(groups), obj["boot_id"])


def dumps(settings: Settings) -> str:
    """Return readable JSON containing only settings, never process arguments."""
    rules = []
    for rule in settings.rules:
        window = rule.window
        rules.append({"name": rule.name, "down": rule.down_pct, "up": rule.up_pct,
                      "start": None if window is None else window.start.strftime("%H:%M"),
                      "end": None if window is None else window.end.strftime("%H:%M")})
    groups = []
    for item in settings.groups:
        group = item.group
        groups.append({"name": group.name, "down": group.down_pct, "up": group.up_pct,
                       "members": [{"pid": pid, "start_ticks": start} for pid, start in zip(group.members, item.starts)]})
    return json.dumps({"version": 1, "interface": settings.iface, "down_budget_bps": settings.down_budget,
                       "up_budget_bps": settings.up_budget, "rules": rules, "groups": groups, "boot_id": settings.boot_id}, indent=2) + "\n"


def read_settings(path) -> Settings:
    """Read a bounded regular UTF-8 settings file and validate it completely."""
    import os
    import stat

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("settings must be a regular file")
            raw = stream.read(1_048_577)
    except OSError as exc:
        raise ValueError(f"cannot read settings: {exc.strerror or exc}") from exc
    if len(raw) > 1_048_576:
        raise ValueError("settings file is too large")
    try:
        return loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError("settings file must be UTF-8") from exc


def write_settings(path, settings: Settings) -> None:
    """Atomically replace a settings file with a private, validated document."""
    import os
    import tempfile
    from pathlib import Path

    content = dumps(settings)
    loads(content)
    destination = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         prefix=".bandctl-", delete=False) as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    except OSError as exc:
        raise ValueError(f"cannot write settings: {exc.strerror or exc}") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def snapshot_settings(iface, down_budget, up_budget, rules, shares, boot_id) -> Settings:
    """Capture settings and verified group identities without process arguments."""
    groups = []
    for group in active_groups(shares):
        starts = tuple(shares[pid].start_ticks for pid in group.members)
        groups.append(GroupSettings(group, starts))
    return Settings(iface, down_budget, up_budget, tuple(rules), tuple(groups), boot_id if groups else None)


def validate_targets(settings: Settings, iface: str, boot_id: str, processes) -> None:
    """Check the interface and all group identities before changing anything."""
    if settings.iface != iface:
        raise ValueError(f"settings use interface {settings.iface}; current interface is {iface}")
    if settings.groups and settings.boot_id != boot_id:
        raise ValueError("group members were recorded on a different boot")
    starts = {proc.pid: proc.start_ticks for proc in processes}
    for item in settings.groups:
        for pid, start in zip(item.group.members, item.starts):
            if starts.get(pid) != start:
                raise ValueError(f"cannot verify group member pid {pid}")


@dataclass(frozen=True)
class GroupChanges:
    """The process members to retain, add, or remove for an import."""

    keep: frozenset[int]
    add: frozenset[int]
    remove: frozenset[int]


def plan_group_changes(settings: Settings, shares) -> GroupChanges:
    """Reject conflicts with individual limits and plan group membership changes."""
    desired = {pid: start for item in settings.groups
               for pid, start in zip(item.group.members, item.starts)}
    existing = {pid: share for pid, share in shares.items() if share.group is not None}
    for pid, start in desired.items():
        share = shares.get(pid)
        if share is not None and share.group is None:
            raise ValueError(f"pid {pid} already has an individual limit")
        if share is not None and share.start_ticks != start:
            raise ValueError(f"group member pid {pid} changed")
    old, new = set(existing), set(desired)
    return GroupChanges(frozenset(old & new), frozenset(new - old), frozenset(old - new))
