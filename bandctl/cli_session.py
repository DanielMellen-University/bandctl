"""In-memory controller for non-interactive shaping sessions."""

from __future__ import annotations

from bandctl.cli import limit_percentages, resolve_process
from bandctl.settings_session import export_session, import_session
from bandctl.shaper import ShaperError


class CliSession:
    """Apply CLI requests through the same shaper and validation as the TUI."""

    def __init__(self, shaper, iface: str, detected_bps: int | None, processes):
        self.shaper = shaper
        self.iface = iface
        self.detected_bps = detected_bps
        self.processes = processes
        self.down_budget: int | None = None
        self.up_budget: int | None = None
        self.rules = []

    def capacities(self) -> tuple[int | None, int | None]:
        return (self.down_budget or self.detected_bps, self.up_budget or self.detected_bps)

    def limit(self, target: str, down: str | None, up: str | None) -> dict:
        down_pct, up_pct = limit_percentages(down, up)
        proc = resolve_process(target, self.processes())
        down_cap, up_cap = self.capacities()
        self.shaper.apply(proc.pid, down_pct, up_pct, down_cap, up_cap, proc.comm,
                          expected_start_ticks=proc.start_ticks)
        return {"pid": proc.pid, "name": proc.comm, "down": down_pct, "up": up_pct}

    def remove(self, target: str) -> dict:
        proc = resolve_process(target, self.processes())
        share = self.shaper.shares.get(proc.pid)
        if share is None:
            raise ShaperError(f"pid {proc.pid} has no limit")
        if share.start_ticks != proc.start_ticks:
            raise ShaperError(f"pid {proc.pid} changed since its limit was applied")
        self.shaper.remove(proc.pid, expected_revision=share.revision)
        return {"pid": proc.pid, "name": proc.comm}

    def export(self, path) -> None:
        export_session(path, self.shaper, self.iface, self.down_budget, self.up_budget, self.rules)

    def import_file(self, path) -> None:
        settings = import_session(path, self.shaper, self.iface, self.detected_bps, self.processes())
        self.down_budget, self.up_budget = settings.down_budget, settings.up_budget
        self.rules = list(settings.rules)
