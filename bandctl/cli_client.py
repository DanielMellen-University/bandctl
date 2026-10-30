"""Start and contact the local non-interactive shaping session."""

from __future__ import annotations

import errno
import subprocess
import sys
import time

from bandctl.cli_transport import request
from bandctl.cli_worker import SOCKET_PATH
from bandctl.iface import check_iface, default_iface
from bandctl.session_lock import SessionLock, SessionLockError


def _ping(path: str, *, match_wildcard: bool = False):
    try:
        response = request(path, {"command": "ping"}, timeout=1.0)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
            return None
        raise
    if not response.get("ok") or not isinstance(response.get("result"), dict):
        raise ValueError("invalid session response")
    iface = response["result"].get("iface")
    if not isinstance(iface, str):
        raise ValueError("invalid session interface")
    if match_wildcard and response["result"].get("match_wildcard") is not True:
        raise ValueError("active session did not enable --match-wildcard; stop it and start a new session")
    return iface


def ensure_session(iface: str | None, *, start: bool, socket_path: str = SOCKET_PATH,
                   match_wildcard: bool = False) -> str:
    """Find the existing session, or start one for a limit or import request."""
    active = _ping(socket_path, match_wildcard=match_wildcard)
    if active is not None:
        if iface is not None and iface != active:
            raise ValueError(f"active session uses interface {active}")
        return active
    if not start:
        raise ValueError("no active CLI session")
    chosen = check_iface(iface or default_iface())
    command = [sys.executable, "-m", "bandctl.cli_worker", "--iface", chosen]
    if match_wildcard:
        command.append("--match-wildcard")
    worker = subprocess.Popen(command,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              start_new_session=True, close_fds=True)
    deadline = time.monotonic() + 5.0
    ready = False
    lock_error = None
    try:
        while time.monotonic() < deadline:
            active = _ping(socket_path, match_wildcard=match_wildcard)
            if active is not None:
                if active != chosen:
                    raise ValueError(f"active session uses interface {active}")
                ready = True
                return active
            if worker.poll() is not None and lock_error is None:
                detail = worker.stderr.read(4096).decode("utf-8", errors="replace").strip() if worker.stderr else ""
                if detail == "bandctl: another bandctl session is already running":
                    # Another CLI worker may own the lock but not have bound its socket yet.
                    lock_error = detail
                else:
                    raise ValueError(detail or "could not start CLI session")
            time.sleep(0.05)
        raise ValueError(lock_error or "CLI session did not start")
    finally:
        if not ready and worker.poll() is None:
            try:
                worker.terminate()
                worker.wait(timeout=3.0)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if worker.stderr is not None:
            worker.stderr.close()


def wait_for_stop(*, lock_path: str = "/run/bandctl.lock", timeout: float = 5.0) -> None:
    """Wait until the worker has finished teardown and released its lock."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lock = SessionLock(lock_path)
        try:
            lock.acquire()
        except SessionLockError as exc:
            if str(exc) != "another bandctl session is already running":
                raise ValueError(str(exc)) from exc
            time.sleep(0.05)
        else:
            lock.release()
            return
    raise ValueError("session cleanup did not finish")
