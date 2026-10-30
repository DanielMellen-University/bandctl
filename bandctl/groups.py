"""Validated definitions for shared process limits."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Group:
    """A named ceiling shared by the listed process IDs."""

    name: str
    down_pct: int | None
    up_pct: int | None
    members: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise ValueError("group name must be text")
        if any(0xD800 <= ord(char) <= 0xDFFF for char in self.name):
            raise ValueError("group name must be valid Unicode")
        if any(ord(char) < 32 or ord(char) == 127 for char in self.name):
            raise ValueError("group name cannot contain control characters")
        name = self.name.strip()
        if not name:
            raise ValueError("enter a group name")
        if self.down_pct is None and self.up_pct is None:
            raise ValueError("enter a download percent, an upload percent, or both")
        for value in (self.down_pct, self.up_pct):
            if value is not None and (type(value) is not int or not 1 <= value <= 100):
                raise ValueError("percent must be from 1 to 100")
        members = tuple(self.members)
        if any(type(pid) is not int or pid <= 0 for pid in members):
            raise ValueError("group members must be positive process IDs")
        if len(set(members)) != len(members):
            raise ValueError("a process cannot appear twice in a group")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "members", members)


def validate_groups(groups: list[Group]) -> None:
    """Reject ambiguous names and processes assigned to multiple groups."""
    names: set[str] = set()
    members: set[int] = set()
    for group in groups:
        name = group.name.casefold()
        if name in names:
            raise ValueError("group names must be unique")
        names.add(name)
        if members.intersection(group.members):
            raise ValueError("a process cannot belong to multiple groups")
        members.update(group.members)


def active_groups(shares) -> list[Group]:
    """Build group definitions from a snapshot of installed process shares."""
    grouped = {}
    for share in shares.values():
        if share.group is None:
            continue
        key = share.group.casefold()
        if key not in grouped:
            grouped[key] = (share, [])
        first, members = grouped[key]
        if (share.group, share.minor, share.down_pct, share.up_pct) != (
            first.group, first.minor, first.down_pct, first.up_pct
        ):
            raise ValueError("group members have conflicting limits")
        members.append(share.pid)
    return [Group(first.group, first.down_pct, first.up_pct, tuple(sorted(members)))
            for first, members in sorted(grouped.values(), key=lambda entry: entry[0].group.casefold())]
