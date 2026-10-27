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


def run_command(args) -> int:
    """Send one CLI operation to the active local session."""
    import json
    import os
    import sys
    from bandctl.cli_client import ensure_session, wait_for_stop
    from bandctl.cli_transport import request
    from bandctl.cli_worker import SOCKET_PATH
    from bandctl.iface import IfaceError
    from bandctl.settings import read_settings

    try:
        if os.geteuid() != 0:
            raise ValueError("root is required for this command")
        if args.command == "limit":
            limit_percentages(args.down, args.up)
            payload = {"command": "limit", "target": args.target, "down": args.down, "up": args.up}
        elif args.command == "remove":
            payload = {"command": "remove", "target": args.target}
        elif args.command in ("import", "export"):
            payload = {"command": args.command, "path": os.path.abspath(args.path)}
        else:
            payload = {"command": "stop"}
        iface = args.iface
        if args.command == "import":
            saved = read_settings(payload["path"])
            if iface is not None and iface != saved.iface:
                raise ValueError("--iface differs from the settings file interface")
            iface = saved.iface
        ensure_session(iface, start=args.command in ("limit", "import"))
        response = request(SOCKET_PATH, payload, timeout=30.0)
        if not response["ok"]:
            raise ValueError(response.get("error", "request failed"))
        if args.command == "stop":
            wait_for_stop()
        result = response.get("result", {})
        warnings = response.get("warnings", [])
        if args.json:
            if warnings:
                result = {**result, "warnings": warnings}
            print(json.dumps(result))
        elif args.command == "limit":
            down = "unlimited" if result["down"] is None else f"{result['down']}%"
            up = "unlimited" if result["up"] is None else f"{result['up']}%"
            print(f"pid {result['pid']}: down {down}, up {up}")
        elif args.command == "remove":
            print(f"removed limit for pid {result['pid']}")
        elif args.command == "stop":
            print("session stopped")
        else:
            verb = "imported" if args.command == "import" else "exported"
            print(f"settings {verb}: {result['path']}")
        if warnings and not args.json:
            for warning in warnings:
                print(f"bandctl: {warning}", file=sys.stderr)
        return 0
    except (ValueError, OSError, IfaceError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
        else:
            print(f"bandctl: {exc}", file=sys.stderr)
        return 1
