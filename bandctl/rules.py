"""Validation and matching for automatic process limits."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AutoRule:
    """A case-insensitive substring of a process name and its limits."""

    name: str
    down_pct: int | None
    up_pct: int | None

    def __post_init__(self) -> None:
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


def first_match(rules: list[AutoRule], process_name: str) -> AutoRule | None:
    """The earliest matching rule wins."""
    return next((rule for rule in rules if rule.matches(process_name)), None)
