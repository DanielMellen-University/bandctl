"""Exclusive ownership of bandctl's host shaping state."""

from __future__ import annotations

import fcntl
import os
import stat


class SessionLockError(RuntimeError):
    """The shaping session is already owned or the lock cannot be opened."""


class SessionLock:
    def __init__(self, path: str = "/run/bandctl.lock") -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            raise SessionLockError("session lock is already held")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        except OSError as exc:
            raise SessionLockError(f"cannot open session lock: {exc.strerror or exc}") from exc
        try:
            regular = stat.S_ISREG(os.fstat(fd).st_mode)
        except OSError as exc:
            os.close(fd)
            raise SessionLockError(f"cannot inspect session lock: {exc.strerror or exc}") from exc
        if not regular:
            os.close(fd)
            raise SessionLockError("session lock must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise SessionLockError("another bandctl session is already running") from exc
        except OSError as exc:
            os.close(fd)
            raise SessionLockError(f"cannot lock session: {exc.strerror or exc}") from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args) -> None:
        self.release()
