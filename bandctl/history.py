"""A bounded, one-second history of measured process rates."""

from __future__ import annotations

import math

_LEVELS = "▁▂▃▄▅▆▇█"
WINDOW = 60


class RateHistory:
    def __init__(self) -> None:
        self._samples: dict[int, dict[int, tuple[float, float]]] = {}

    def forget(self, pid: int) -> None:
        self._samples.pop(pid, None)

    def record(self, now: float, pids: set[int], rates: dict[int, tuple[float, float]] | None) -> None:
        """Record a second, or leave a gap when the measurement failed."""
        second = int(now)
        for pid in list(self._samples):
            if pid not in pids:
                del self._samples[pid]
        for pid in pids:
            samples = self._samples.setdefault(pid, {})
            for old in list(samples):
                if old <= second - WINDOW:
                    del samples[old]
            if rates is not None:
                down, up = rates.get(pid, (0.0, 0.0))
                samples[second] = (max(0.0, down), max(0.0, up))
            else:
                samples.pop(second, None)

    def graph(self, pid: int | None, direction: int, now: float, width: int) -> str:
        """Render the last minute left to right, grouping seconds when needed."""
        width = min(WINDOW, max(0, width))
        if not width:
            return ""
        second = int(now)
        samples = self._samples.get(pid, {})
        values = [samples.get(second - WINDOW + 1 + i) for i in range(WINDOW)]
        groups = []
        for column in range(width):
            start = column * WINDOW // width
            end = (column + 1) * WINDOW // width
            measured = [pair[direction] for pair in values[start:end] if pair is not None]
            groups.append(max(measured) if measured else None)
        peak = max((value for value in groups if value is not None), default=0.0)
        return "".join(
            " " if value is None else _LEVELS[min(7, math.floor(value / peak * 7))] if peak else _LEVELS[0]
            for value in groups
        )
