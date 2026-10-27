"""In-memory controller for non-interactive shaping sessions."""

from __future__ import annotations

import time
from datetime import datetime

from bandctl.rules import RuleTracker, ScheduledOwnership
from bandctl.cli import limit_percentages, resolve_process
from bandctl.settings_session import export_session, import_session
from bandctl.shaper import ShaperError


class CliSession:
    """Apply CLI requests through the same shaper and validation as the TUI."""

    def __init__(self, shaper, iface: str, detected_bps: int | None, processes, *, speed_source=None):
        self.shaper = shaper
        self.iface = iface
        self.detected_bps = detected_bps
        self.processes = processes
        self.speed_source = speed_source
        self.down_budget: int | None = None
        self.up_budget: int | None = None
        self.rules = []
        self.warnings: list[str] = []
        self._tracker = RuleTracker()
        self._scheduled = ScheduledOwnership()

    def capacities(self) -> tuple[int | None, int | None]:
        return (self.down_budget or self.detected_bps, self.up_budget or self.detected_bps)

    def limit(self, target: str, down: str | None, up: str | None) -> dict:
        down_pct, up_pct = limit_percentages(down, up)
        proc = resolve_process(target, self.processes())
        down_cap, up_cap = self.capacities()
        self.shaper.apply(proc.pid, down_pct, up_pct, down_cap, up_cap, proc.comm,
                          expected_start_ticks=proc.start_ticks)
        self._scheduled.forget(proc)
        self._tracker.applied(proc)
        return {"pid": proc.pid, "name": proc.comm, "down": down_pct, "up": up_pct}

    def remove(self, target: str) -> dict:
        proc = resolve_process(target, self.processes())
        share = self.shaper.shares.get(proc.pid)
        if share is None:
            raise ShaperError(f"pid {proc.pid} has no limit")
        if share.start_ticks != proc.start_ticks:
            raise ShaperError(f"pid {proc.pid} changed since its limit was applied")
        self.shaper.remove(proc.pid, expected_revision=share.revision)
        self._scheduled.forget(proc)
        self._tracker.applied(proc)
        return {"pid": proc.pid, "name": proc.comm}

    def export(self, path) -> None:
        export_session(path, self.shaper, self.iface, self.down_budget, self.up_budget, self.rules)

    def import_file(self, path) -> None:
        settings = import_session(path, self.shaper, self.iface, self.detected_bps, self.processes())
        self.down_budget, self.up_budget = settings.down_budget, settings.up_budget
        self.rules = list(settings.rules)

    def refresh(self, *, now: float | None = None, current=None) -> list[str]:
        """Refresh socket filters and reconcile automatic rules for the session."""
        now = time.monotonic() if now is None else now
        current = datetime.now().astimezone().time() if current is None else current
        messages = []
        if self.speed_source is not None:
            detected = self.speed_source()
            if detected != self.detected_bps:
                previous = self.detected_bps
                self.detected_bps = detected
                try:
                    self.shaper.set_capacities(*self.capacities())
                except (ShaperError, OSError) as exc:
                    self.detected_bps = previous
                    messages.append(f"capacity update: {exc}")
        self.shaper.sample()
        if self.shaper.flow_warning:
            messages.append(f"flow refresh: {self.shaper.flow_warning}")
        processes = self.processes()
        shares = self.shaper.shares
        limits = {pid: (share.down_pct, share.up_pct, share.revision) for pid, share in shares.items()}
        for proc, revision in self._scheduled.expired(processes, self.rules, limits, current, now):
            try:
                self.shaper.remove(proc.pid, expected_revision=revision)
            except (ShaperError, OSError) as exc:
                self._scheduled.failed(proc, now)
                messages.append(f"scheduled limit for pid {proc.pid}: {exc}")
            else:
                self._scheduled.forget(proc)
                self._tracker.released(proc)
                messages.append(f"schedule ended for pid {proc.pid}")
        candidates = self._tracker.candidates(processes, self.rules, set(self.shaper.shares), now, current)
        for proc, rule in candidates:
            try:
                message, revision = self.shaper.apply_tracked(
                    proc.pid, rule.down_pct, rule.up_pct, *self.capacities(), proc.comm,
                    expected_start_ticks=proc.start_ticks, only_if_unlimited=True,
                )
            except (ShaperError, OSError) as exc:
                self._tracker.failed(proc, now)
                messages.append(f"auto rule for pid {proc.pid}: {exc}")
            else:
                self._tracker.applied(proc)
                self._scheduled.applied(proc, rule, revision)
                messages.append(f"auto rule: {message}")
        self.warnings = [message for message in messages if message.startswith(
            ("capacity update:", "flow refresh:", "scheduled limit for pid", "auto rule for pid")
        )]
        return messages
