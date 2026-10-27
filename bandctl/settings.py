"""Validated interchange format for explicitly exported settings."""

from __future__ import annotations

import json
from dataclasses import dataclass

from bandctl.groups import Group, validate_groups
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

    def __post_init__(self) -> None:
        if (not isinstance(self.iface, str) or not self.iface or self.iface in (".", "..")
                or len(self.iface.encode("utf-8")) > 15 or "/" in self.iface
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in self.iface)):
            raise ValueError("invalid interface name")
        for budget in (self.down_budget, self.up_budget):
            if budget is not None and (type(budget) is not int or budget < 1):
                raise ValueError("budgets must be positive whole numbers of bits per second")
        validate_groups([item.group for item in self.groups])


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
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid settings JSON") from exc
    obj = _object(raw, {"version", "interface", "down_budget_bps", "up_budget_bps", "rules", "groups"}, "settings")
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
    return Settings(obj["interface"], obj["down_budget_bps"], obj["up_budget_bps"], tuple(rules), tuple(groups))


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
                       "up_budget_bps": settings.up_budget, "rules": rules, "groups": groups}, indent=2) + "\n"
