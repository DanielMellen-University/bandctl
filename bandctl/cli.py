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
