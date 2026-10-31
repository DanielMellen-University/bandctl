"""``python -m bandctl``."""

from __future__ import annotations

import argparse
import atexit
import os
import signal
import sys

from bandctl import __version__
from bandctl.app import BandctlApp
from bandctl.cli import list_processes, run_command
from bandctl.iface import IfaceError, check_iface
from bandctl.settings import read_settings
from bandctl.session_lock import SessionLock, SessionLockError
from bandctl.text import terminal_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bandctl",
        description=(
            "Terminal UI for per-process download and upload shares. "
            "A share is a percent of link capacity. Rules exist only while "
            "bandctl is running, and exit removes them."
        ),
    )
    parser.add_argument("--version", action="version", version=f"bandctl {__version__}")
    parser.add_argument(
        "--iface",
        help="Interface to shape. Default: the device on the IPv4 default route.",
    )
    parser.add_argument("--config", help="Import this settings file at startup.")
    parser.add_argument(
        "--match-wildcard", action="store_true",
        help="Match wildcard-bound UDP by port; may also shape other processes sharing that port.",
    )
    commands = parser.add_subparsers(dest="command")
    list_parser = commands.add_parser("list", help="List running processes.")
    list_parser.add_argument("--json", action="store_true", help="Write JSON output.")
    limit_parser = commands.add_parser("limit", help="Set a process limit.")
    limit_parser.add_argument("target", help="PID or exact process name.")
    limit_parser.add_argument("--down", help="Download percent from 1 to 100.")
    limit_parser.add_argument("--up", help="Upload percent from 1 to 100.")
    limit_parser.add_argument("--json", action="store_true", help="Write JSON output.")
    remove_parser = commands.add_parser("remove", help="Remove a process limit.")
    remove_parser.add_argument("target", help="PID or exact process name.")
    remove_parser.add_argument("--json", action="store_true", help="Write JSON output.")
    for command in ("import", "export"):
        file_parser = commands.add_parser(command, help=f"{command.capitalize()} settings.")
        file_parser.add_argument("path", help="Settings file path.")
        file_parser.add_argument("--json", action="store_true", help="Write JSON output.")
    stop_parser = commands.add_parser("stop", help="Stop the CLI session and remove its limits.")
    stop_parser.add_argument("--json", action="store_true", help="Write JSON output.")
    args = parser.parse_args(argv)
    if args.command == "list":
        if args.config is not None:
            parser.error("--config is only available when opening the TUI")
        sys.stdout.write(list_processes(json_output=args.json))
        return 0
    if args.command is not None:
        if args.config is not None:
            parser.error("--config is only available when opening the TUI")
        return run_command(args)
    if args.config is not None:
        try:
            settings = read_settings(args.config)
        except ValueError as exc:
            parser.error(terminal_text(str(exc)))
        if args.iface is not None and args.iface != settings.iface:
            parser.error("--iface differs from the settings file interface")
        args.iface = settings.iface
    if args.iface is not None:
        try:
            check_iface(args.iface)
        except IfaceError as exc:
            parser.error(terminal_text(str(exc)))
    lock = None
    if os.geteuid() == 0:
        lock = SessionLock()
        try:
            lock.acquire()
        except SessionLockError as exc:
            print(f"bandctl: {terminal_text(str(exc))}", file=sys.stderr)
            return 1
    try:
        app = BandctlApp(iface=args.iface, startup_settings=args.config, match_wildcard=args.match_wildcard)
        stop_signum = None

        def _stop(signum, _frame) -> None:
            nonlocal stop_signum
            if stop_signum is None:
                stop_signum = signum
                app.exit()

        signal.signal(signal.SIGTERM, _stop)
        atexit.register(_cleanup, app)
        try:
            app.run()
        finally:
            _cleanup(app)
            for error in app.cleanup_errors:
                print(f"bandctl: cleanup failed: {terminal_text(error)}", file=sys.stderr)
    finally:
        if lock is not None:
            lock.release()
    if stop_signum is not None:
        return 128 + stop_signum
    return 1 if app.cleanup_errors else 0


def _cleanup(app: BandctlApp) -> None:
    shaper = app.shaper
    if shaper is not None and app.is_root:
        app.cleanup_errors.extend(shaper.teardown())


if __name__ == "__main__":
    sys.exit(main())
