"""Session locking with temporary files, no host shaping."""
import os
import tempfile
import unittest
from pathlib import Path

from bandctl.session_lock import SessionLock, SessionLockError


class SessionLockTests(unittest.TestCase):
    def test_exclusive_lock_releases_for_next_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "bandctl.lock")
            first, second = SessionLock(path), SessionLock(path)
            first.acquire()
            with self.assertRaisesRegex(SessionLockError, "another bandctl"):
                second.acquire()
            with self.assertRaisesRegex(SessionLockError, "already held"):
                first.acquire()
            first.release()
            with second:
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            first.acquire()
            first.release()
            first.release()

    def test_refuses_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.write_text("unchanged")
            link = Path(directory) / "lock"
            link.symlink_to(target)
            with self.assertRaisesRegex(SessionLockError, "cannot open"):
                SessionLock(str(link)).acquire()
            self.assertEqual(target.read_text(), "unchanged")

    def test_context_manager_releases_after_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "lock")
            with self.assertRaises(ValueError):
                with SessionLock(path):
                    raise ValueError("failed")
            with SessionLock(path):
                pass
