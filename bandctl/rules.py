"""Validation and matching for automatic process limits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from bandctl.procscan import ProcessInfo
from bandctl.schedule import DailyWindow


@dataclass(frozen=True)
class AutoRule:
    """A case-insensitive substring of a process name and its limits."""

    name: str
    down_pct: int | None
    up_pct: int | None
    window: DailyWindow | None = None

    def __post_init__(self) -> None:
        if any(0xD800 <= ord(char) <= 0xDFFF for char in self.name):
            raise ValueError("process name must be valid Unicode")
        if any(ord(char) < 32 or ord(char) == 127 for char in self.name):
            raise ValueError("process name cannot contain control characters")
        name = self.name.strip()
        if not name:
            raise ValueError("enter a process name")
        if self.down_pct is None and self.up_pct is None:
            raise ValueError("enter a download percent, an upload percent, or both")
        for value in (self.down_pct, self.up_pct):
            if value is not None and (type(value) is not int or not 1 <= value <= 100):
                raise ValueError("percent must be from 1 to 100")
        object.__setattr__(self, "name", name)

    def matches(self, process_name: str) -> bool:
        return self.name.casefold() in process_name.casefold()

    def active(self, current: time | None) -> bool:
        return self.window is None or (current is not None and self.window.contains(current))


def first_match(rules: list[AutoRule], process_name: str, current: time | None = None) -> AutoRule | None:
    """The earliest active matching rule wins."""
    return next((rule for rule in rules if rule.active(current) and rule.matches(process_name)), None)


class RuleTracker:
    """Select matching process instances without undoing a manual removal."""

    def __init__(self) -> None:
        self._handled: set[tuple[int, int]] = set()
        self._retry_at: dict[tuple[int, int], float] = {}
        self._last_seen: dict[tuple[int, int], float] = {}

    def candidates(
        self, processes: list[ProcessInfo], rules: list[AutoRule], limited: set[int], now: float,
        current: time | None = None,
    ) -> list[tuple[ProcessInfo, AutoRule]]:
        found = []
        for proc in processes:
            if proc.start_ticks is None:
                continue
            key = (proc.pid, proc.start_ticks)
            self._last_seen[key] = now
            rule = first_match(rules, proc.comm, current)
            if rule is None:
                continue
            if proc.pid in limited:
                self._handled.add(key)
            if key not in self._handled and now >= self._retry_at.get(key, 0):
                found.append((proc, rule))
        for key, seen in list(self._last_seen.items()):
            if now - seen > 300:
                self._last_seen.pop(key, None)
                self._handled.discard(key)
                self._retry_at.pop(key, None)
        return found

    def applied(self, proc: ProcessInfo) -> None:
        if proc.start_ticks is not None:
            key = (proc.pid, proc.start_ticks)
            self._handled.add(key)
            self._retry_at.pop(key, None)

    def failed(self, proc: ProcessInfo, now: float) -> None:
        if proc.start_ticks is not None:
            self._retry_at[(proc.pid, proc.start_ticks)] = now + 15

    def released(self, proc: ProcessInfo) -> None:
        if proc.start_ticks is not None:
            key = (proc.pid, proc.start_ticks)
            self._handled.discard(key)
            self._retry_at.pop(key, None)


class ScheduledOwnership:
    """Track automatic limits so active rules can change with the clock."""

    def __init__(self) -> None:
        self._owned: dict[tuple[int, int], tuple[AutoRule, int]] = {}
        self._retry_at: dict[tuple[int, int], float] = {}

    def applied(self, proc: ProcessInfo, rule: AutoRule, revision: int) -> None:
        if proc.start_ticks is not None:
            self._owned[(proc.pid, proc.start_ticks)] = (rule, revision)

    def forget(self, proc: ProcessInfo) -> None:
        if proc.start_ticks is not None:
            key = (proc.pid, proc.start_ticks)
            self._owned.pop(key, None)
            self._retry_at.pop(key, None)

    def failed(self, proc: ProcessInfo, now: float) -> None:
        if proc.start_ticks is not None:
            self._retry_at[(proc.pid, proc.start_ticks)] = now + 15

    def expired(
        self, processes: list[ProcessInfo], rules: list[AutoRule],
        limits: dict[int, tuple[int | None, int | None, int]], current: time, now: float,
    ) -> list[tuple[ProcessInfo, int]]:
        instances = {(proc.pid, proc.start_ticks): proc for proc in processes if proc.start_ticks is not None}
        by_pid = {proc.pid: proc for proc in processes}
        found = []
        for key, (rule, revision) in list(self._owned.items()):
            proc = instances.get(key)
            replacement = by_pid.get(key[0])
            changed_instance = replacement is not None and replacement.start_ticks is not None and replacement.start_ticks != key[1]
            if limits.get(key[0]) != (rule.down_pct, rule.up_pct, revision) or changed_instance:
                self._owned.pop(key, None)
                self._retry_at.pop(key, None)
                continue
            if proc is None:
                continue
            if rule.window is None and rule not in rules:
                self.forget(proc)
                continue
            if first_match(rules, proc.comm, current) != rule and now >= self._retry_at.get(key, 0):
                found.append((proc, revision))
        return found
