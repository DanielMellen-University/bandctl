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


class MainLockTests(unittest.TestCase):
    def test_root_tui_refuses_a_contended_session(self):
        import contextlib
        import io
        from unittest.mock import patch
        from bandctl.__main__ import main

        stderr = io.StringIO()
        with patch("bandctl.__main__.os.geteuid", return_value=0), \
             patch("bandctl.__main__.SessionLock") as lock, \
             patch("bandctl.__main__.BandctlApp") as app, contextlib.redirect_stderr(stderr):
            lock.return_value.acquire.side_effect = SessionLockError("another bandctl session is already running")
            self.assertEqual(main([]), 1)
        app.assert_not_called()
        self.assertIn("another bandctl session", stderr.getvalue())

    def test_initialization_failure_releases_lock(self):
        from unittest.mock import Mock, patch
        from bandctl.__main__ import main

        lock = Mock()
        with patch("bandctl.__main__.os.geteuid", return_value=0), \
             patch("bandctl.__main__.SessionLock", return_value=lock), \
             patch("bandctl.__main__.BandctlApp", side_effect=RuntimeError("failed")):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                main([])
        lock.release.assert_called_once()

    def test_tui_reports_cleanup_errors_after_unmount(self):
        import contextlib
        import io
        from unittest.mock import Mock, patch
        from bandctl.app import BandctlApp
        from bandctl.__main__ import main

        shaper = Mock()
        shaper.teardown.side_effect = [["cgroup cleanup failed"], []]
        app = BandctlApp(iface="eth0", shaper=shaper, is_root=True)
        stderr = io.StringIO()
        with patch("bandctl.__main__.os.geteuid", return_value=0), \
             patch("bandctl.__main__.SessionLock"), \
             patch("bandctl.__main__.BandctlApp", return_value=app), \
             patch("bandctl.__main__.signal.signal"), patch("bandctl.__main__.atexit.register"), \
             patch.object(app, "run", side_effect=app.on_unmount), contextlib.redirect_stderr(stderr):
            self.assertEqual(main([]), 1)
        self.assertIn("cgroup cleanup failed", stderr.getvalue())

    def test_sigterm_reports_cleanup_errors(self):
        import contextlib
        import io
        from unittest.mock import Mock, patch
        from bandctl.app import BandctlApp
        from bandctl.__main__ import main

        shaper = Mock()
        shaper.teardown.side_effect = [["restore failed"], []]
        app = BandctlApp(iface="eth0", shaper=shaper, is_root=True)
        stderr = io.StringIO()
        with patch("bandctl.__main__.os.geteuid", return_value=0), \
             patch("bandctl.__main__.SessionLock"), \
             patch("bandctl.__main__.BandctlApp", return_value=app), \
             patch("bandctl.__main__.signal.signal") as signal, \
             patch("bandctl.__main__.atexit.register"), contextlib.redirect_stderr(stderr):
            def stop_app():
                signal.call_args.args[1](15, None)

            with patch.object(app, "run", side_effect=stop_app):
                self.assertEqual(main([]), 143)
        self.assertEqual(stderr.getvalue().count("restore failed"), 1)

    def test_sigterm_during_qdisc_change_waits_for_operation_before_cleanup(self):
        from unittest.mock import Mock, patch
        from bandctl.__main__ import main
        from test_shaper import _shaper

        shaper, host = _shaper()
        app = Mock()
        app.is_root = True
        app.shaper = shaper
        app.cleanup_errors = []
        run = host.run
        with patch("bandctl.__main__.os.geteuid", return_value=0), \
             patch("bandctl.__main__.SessionLock"), \
             patch("bandctl.__main__.BandctlApp", return_value=app), \
             patch("bandctl.__main__.signal.signal") as signal, \
             patch("bandctl.__main__.atexit.register"):
            def stop_after_replace(args, input_text=None):
                result = run(args, input_text)
                if args[:4] == ["tc", "qdisc", "replace", "dev"]:
                    signal.call_args.args[1](15, None)
                    signal.call_args.args[1](15, None)
                return result

            def run_app():
                shaper.apply(42, None, 10, None, 100_000_000)

            with patch.object(host, "run", side_effect=stop_after_replace), \
                 patch.object(app, "run", side_effect=run_app):
                self.assertEqual(main([]), 143)
        app.exit.assert_called_once()
        commands = [" ".join(args) for args, _ in host.commands]
        self.assertIn("tc qdisc add dev eth0 root fq_codel", commands)
        self.assertEqual(host.files["/proc/42/cgroup"], "0::/user.slice/user-1000.slice/session-1.scope\n")
        self.assertNotIn("/run/bandctl.json", host.files)

    def test_root_tui_cleans_up_before_unlocking(self):
        from unittest.mock import Mock, patch
        from bandctl.__main__ import main

        order = []
        app = Mock()
        app.is_root = True
        app.cleanup_errors = []
        app.shaper.teardown.side_effect = lambda: order.append("cleanup") or []
        lock = Mock()
        lock.release.side_effect = lambda: order.append("unlock")
        with patch("bandctl.__main__.os.geteuid", return_value=0), \
             patch("bandctl.__main__.SessionLock", return_value=lock), \
             patch("bandctl.__main__.BandctlApp", return_value=app), \
             patch("bandctl.__main__.signal.signal"), patch("bandctl.__main__.atexit.register"):
            self.assertEqual(main([]), 0)
        self.assertEqual(order, ["cleanup", "unlock"])
