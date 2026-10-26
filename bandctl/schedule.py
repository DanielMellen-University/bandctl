"""Daily local-time windows for scheduled limits."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time

_CLOCK = re.compile(r"^([0-9]{2}):([0-9]{2})$")


def parse_time(text: str) -> time:
    """Parse a 24-hour clock time written as HH:MM."""
    match = _CLOCK.fullmatch(text.strip())
    if match is None:
        raise ValueError("enter a time as HH:MM")
    hour, minute = (int(part) for part in match.groups())
    if hour > 23 or minute > 59:
        raise ValueError("time must be from 00:00 to 23:59")
    return time(hour, minute)


@dataclass(frozen=True)
class DailyWindow:
    start: time
    end: time

    def __post_init__(self) -> None:
        if self.start.tzinfo is not None or self.end.tzinfo is not None:
            raise ValueError("use local clock times without a timezone")
        if self.start.second or self.start.microsecond or self.end.second or self.end.microsecond:
            raise ValueError("use whole-minute times")
        if self.start == self.end:
            raise ValueError("start and end must differ")

    def contains(self, current: time) -> bool:
        """Start is inclusive and end is exclusive, including across midnight."""
        current = current.replace(tzinfo=None)
        if self.start < self.end:
            return self.start <= current < self.end
        return current >= self.start or current < self.end
