"""Percent shares, bit/byte display, and HTB rate splits.

A share is an integer percent of one direction's capacity. The capacity is a
link speed or a budget the user typed. 1 Mbit/s is 1_000_000 bit/s, and
1 MB/s is 8_000_000 bit/s. The unit toggle changes presentation only.
"""

from __future__ import annotations

from enum import Enum
import math


class Unit(Enum):
    MBIT = "Mbit/s"
    MB = "MB/s"


class RateError(ValueError):
    """A percent or a budget string the user typed is not usable."""


def parse_percent(text: str, *, allow_empty: bool = False) -> int | None:
    """Return 1..100, or None when *text* is empty and *allow_empty* is set."""
    raw = text.strip()
    if raw.endswith("%"):
        raw = raw[:-1].strip()
    if not raw:
        if allow_empty:
            return None
        raise RateError("enter a percent from 1 to 100")
    if not raw.isdigit():
        raise RateError(f"{text.strip()!r} is not a whole-number percent")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RateError("enter a whole-number percent from 1 to 100") from exc
    if value < 1 or value > 100:
        raise RateError("percent must be from 1 to 100")
    return value


def parse_budget(text: str, unit: Unit) -> int | None:
    """Parse a capacity in the active unit. Empty means 'use the detected speed'."""
    raw = text.strip().replace("_", "")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise RateError("budget must be a number, in the unit shown in the header") from exc
    bps = value * (1_000_000 if unit is Unit.MBIT else 8_000_000)
    if not math.isfinite(bps) or bps < 1:
        raise RateError("budget must be finite and at least 1 bit/s")
    return int(bps)


def percent_to_bps(percent: int, capacity_bps: int) -> int:
    if percent < 1 or percent > 100:
        raise RateError("percent must be from 1 to 100")
    if capacity_bps <= 0:
        raise RateError("capacity must be greater than zero")
    return capacity_bps * percent // 100


def format_rate(bps: float, unit: Unit) -> str:
    """Absolute rate in the active unit, for table cells and bar captions."""
    if bps < 0:
        bps = 0
    if unit is Unit.MBIT:
        value = bps / 1_000_000
        suffix = "Mbit/s"
    else:
        value = bps / 8_000_000
        suffix = "MB/s"
    if value >= 100:
        return f"{value:.0f} {suffix}"
    if value >= 10:
        return f"{value:.1f} {suffix}"
    return f"{value:.2f} {suffix}"


def format_budget_input(bps: int, unit: Unit) -> str:
    """Number to put back into a budget field after the unit toggle."""
    value = bps / 1_000_000 if unit is Unit.MBIT else bps / 8_000_000
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text


def format_bytes(count: int) -> str:
    """Format a byte count using decimal units."""
    count = max(0, count)
    if count < 1_000:
        return f"{count} B"
    value = float(count)
    suffix = "B"
    for suffix in ("kB", "MB", "GB", "TB", "PB", "EB"):
        value /= 1_000
        if value < 1_000:
            break
    if value >= 100:
        return f"{value:.0f} {suffix}"
    if value >= 10:
        return f"{value:.1f} {suffix}"
    return f"{value:.2f} {suffix}"


def over_commit_message(direction: str, percents: list[int]) -> str | None:
    total = sum(percents)
    if total <= 100:
        return None
    return (
        f"{direction} shares add up to {total}%. "
        "Each ceiling still holds. Guaranteed rates are scaled so they fit the link."
    )


def htb_plan(ceil_bps: list[int], parent_bps: int) -> tuple[list[int], list[int], int]:
    """Split one direction into HTB ``rate`` / ``ceil`` values, in kbit.

    Returns ``(rate_kbit, ceil_kbit, default_rate_kbit)``.

    Each ceil is the requested share of *parent_bps*, capped at the parent.
    Rates equal those ceils when they fit and still leave 1 kbit for the
    default class (unmarked traffic). When the ceils sum past that budget,
    rates shrink in proportion and the ceils stay. The default class gets
    whatever rate is left. Its ceil is the whole parent; the caller sets that.
    """
    if parent_bps <= 0:
        raise RateError("capacity must be greater than zero")
    parent = max((parent_bps + 999) // 1000, 2)
    ceils = [min(max((c + 999) // 1000, 1), parent) if c > 0 else 0 for c in ceil_bps]
    budget = parent - 1
    total = sum(ceils)
    if total == 0:
        return [], [], parent
    if total <= budget:
        rates = list(ceils)
    else:
        raw = [budget * c / total for c in ceils]
        rates = [int(part) for part in raw]
        for index, rate in enumerate(rates):
            if rate < 1 and ceils[index] > 0:
                rates[index] = 1
        while sum(rates) > budget:
            index = max(range(len(rates)), key=lambda i: rates[i])
            if rates[index] <= 1:
                break
            rates[index] -= 1
        leftover = budget - sum(rates)
        order = sorted(
            range(len(rates)),
            key=lambda i: (raw[i] - int(raw[i]), ceils[i]),
            reverse=True,
        )
        for index in order:
            if leftover <= 0:
                break
            if rates[index] < ceils[index]:
                rates[index] += 1
                leftover -= 1
    for index, rate in enumerate(rates):
        if rate > ceils[index]:
            rates[index] = ceils[index]
    default = parent - sum(rates)
    if default < 1:
        default = 1
    return rates, ceils, default
