"""Own a non-interactive shaping session until it is stopped."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import stat
import sys

from bandctl.cli_session import CliSession
from bandctl.cli_transport import serve
from bandctl.iface import IfaceError, check_iface, link_speed_bps
from bandctl.procscan import scan_processes
from bandctl.session_lock import SessionLock, SessionLockError
from bandctl.shaper import Shaper, ShaperError

SOCKET_PATH = "/run/bandctl.sock"


def _remove_stale_socket(path: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
        raise RuntimeError("session socket path is not an owned socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        try:
            probe.connect(path)
        except ConnectionRefusedError:
            pass
        except OSError as exc:
            raise RuntimeError(f"cannot check session socket: {exc}") from exc
        else:
            raise RuntimeError("session socket is already in use")
    current = os.lstat(path)
    if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
        raise RuntimeError("session socket changed")
    os.unlink(path)


def run_worker(iface: str, *, lock_path: str = "/run/bandctl.lock", socket_path: str = SOCKET_PATH,
               host=None, processes=scan_processes, detected_bps=None) -> None:
    """Hold the session lock until the socket closes and shaping is removed."""
    with SessionLock(lock_path):
        _remove_stale_socket(socket_path)
        shaper = Shaper(iface, host=host)
        try:
            shaper.recover()
            capacity = link_speed_bps(iface) if detected_bps is None else detected_bps
            session = CliSession(shaper, iface, capacity, processes)
            serve(session, socket_path, on_stop=shaper.teardown)
        finally:
            shaper.teardown()


def _stop(signum, _frame) -> None:
    raise SystemExit(128 + signum)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="bandctl session")
    parser.add_argument("--iface", required=True)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("bandctl: root is required for limits", file=sys.stderr)
        return 1
    signal.signal(signal.SIGTERM, _stop)
    try:
        check_iface(args.iface)
        run_worker(args.iface)
    except (IfaceError, SessionLockError, ShaperError, OSError, RuntimeError) as exc:
        print(f"bandctl: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
