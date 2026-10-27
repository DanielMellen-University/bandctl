"""Non-interactive commands for bandctl."""

from __future__ import annotations

import json

from bandctl.procscan import scan_processes


def list_processes(*, json_output: bool = False, processes=None) -> str:
    """Return a process list without including command-line arguments."""
    if processes is None:
        processes = scan_processes()
    rows = [{"pid": proc.pid, "name": proc.comm, "user": proc.user, "sockets": proc.sockets}
            for proc in sorted(processes, key=lambda proc: proc.pid)]
    if json_output:
        return json.dumps(rows, indent=2) + "\n"
    def plain(value: str) -> str:
        return "".join("?" if ord(char) < 32 or ord(char) == 127 else char for char in value)

    lines = ["PID      USER         NAME"]
    lines.extend(f"{row['pid']:<8} {plain(row['user']):<12} {plain(row['name'])}" for row in rows)
    return "\n".join(lines) + "\n"


def resolve_process(target: str, processes):
    """Resolve one PID or exact process name, refusing ambiguity and stale identities."""
    target = target.strip()
    if not target:
        raise ValueError("enter a PID or process name")
    if target.isascii() and target.isdigit():
        matches = [proc for proc in processes if proc.pid == int(target)]
    else:
        matches = [proc for proc in processes if proc.comm.casefold() == target.casefold()]
    if not matches:
        raise ValueError(f"no process matches {target!r}")
    if len(matches) != 1:
        raise ValueError(f"more than one process matches {target!r}; use a PID")
    if matches[0].start_ticks is None:
        raise ValueError(f"cannot verify pid {matches[0].pid}")
    return matches[0]


def limit_percentages(down: str | None, up: str | None) -> tuple[int | None, int | None]:
    """Validate the same whole-number shares accepted by the TUI."""
    from bandctl.rates import parse_percent

    down_pct = parse_percent(down, allow_empty=False) if down is not None else None
    up_pct = parse_percent(up, allow_empty=False) if up is not None else None
    if down_pct is None and up_pct is None:
        raise ValueError("enter a download percent, an upload percent, or both")
    return down_pct, up_pct
