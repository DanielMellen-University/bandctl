"""CLI session discovery and startup without launching a real worker."""
import errno
import io
import socket
import subprocess
import unittest
from unittest.mock import Mock, patch

from bandctl.cli_client import _ping, ensure_session, wait_for_stop
from bandctl.session_lock import SessionLockError


class ClientTests(unittest.TestCase):
    def test_only_missing_or_stale_socket_allows_a_new_session(self):
        for error in (FileNotFoundError(errno.ENOENT, "missing"),
                      ConnectionRefusedError(errno.ECONNREFUSED, "stale")):
            with self.subTest(error=error), patch("bandctl.cli_client.request", side_effect=error):
                self.assertIsNone(_ping("socket"))

        for error in (PermissionError(errno.EACCES, "denied"), socket.timeout("timed out")):
            with self.subTest(error=error), \
                 patch("bandctl.cli_client.request", side_effect=error), \
                 patch("bandctl.cli_client.subprocess.Popen") as start:
                with self.assertRaises(type(error)):
                    ensure_session("eth0", start=True)
                start.assert_not_called()

    def test_invalid_ping_response_does_not_start_another_session(self):
        for response in ({"ok": False}, {"ok": True, "result": None},
                         {"ok": True, "result": {"iface": None}}):
            with self.subTest(response=response), \
                 patch("bandctl.cli_client.request", return_value=response), \
                 patch("bandctl.cli_client.subprocess.Popen") as start:
                with self.assertRaisesRegex(ValueError, "invalid session"):
                    ensure_session("eth0", start=True)
                start.assert_not_called()

    def test_explicit_wildcard_option_requires_an_enabled_session(self):
        with patch("bandctl.cli_client.request", return_value={"ok": True, "result": {"iface": "eth0", "match_wildcard": False}}):
            self.assertEqual(_ping("socket"), "eth0")
            with self.assertRaisesRegex(ValueError, "did not enable --match-wildcard"):
                _ping("socket", match_wildcard=True)
        with patch("bandctl.cli_client.request", return_value={"ok": True, "result": {"iface": "eth0", "match_wildcard": True}}):
            self.assertEqual(_ping("socket", match_wildcard=True), "eth0")
            self.assertEqual(_ping("socket"), "eth0")

    def test_wildcard_option_is_passed_to_new_worker(self):
        worker = Mock()
        worker.stderr = io.BytesIO()
        with patch("bandctl.cli_client._ping", side_effect=[None, "eth0"]), \
             patch("bandctl.cli_client.check_iface", return_value="eth0"), \
             patch("bandctl.cli_client.subprocess.Popen", return_value=worker) as start:
            self.assertEqual(ensure_session("eth0", start=True, match_wildcard=True), "eth0")
        self.assertIn("--match-wildcard", start.call_args.args[0])

    def test_existing_session_checks_requested_interface(self):
        with patch("bandctl.cli_client._ping", return_value="eth0"):
            self.assertEqual(ensure_session(None, start=False), "eth0")
            with self.assertRaisesRegex(ValueError, "uses interface eth0"):
                ensure_session("wlan0", start=False)

    def test_no_existing_session_requires_explicit_start(self):
        with patch("bandctl.cli_client._ping", return_value=None), \
             patch("bandctl.cli_client.subprocess.Popen") as start:
            with self.assertRaisesRegex(ValueError, "no active"):
                ensure_session(None, start=False)
        start.assert_not_called()

    def test_failed_start_reports_worker_error(self):
        worker = Mock()
        worker.stderr = io.BytesIO(b"lock is held")
        worker.poll.return_value = 1
        with patch("bandctl.cli_client._ping", return_value=None), \
             patch("bandctl.cli_client.check_iface", return_value="eth0"), \
             patch("bandctl.cli_client.subprocess.Popen", return_value=worker):
            with self.assertRaisesRegex(ValueError, "lock is held"):
                ensure_session("eth0", start=True)
        worker.terminate.assert_not_called()

    def test_start_timeout_terminates_its_own_worker(self):
        worker = Mock()
        worker.stderr = io.BytesIO()
        worker.poll.return_value = None
        with patch("bandctl.cli_client._ping", return_value=None), \
             patch("bandctl.cli_client.check_iface", return_value="eth0"), \
             patch("bandctl.cli_client.subprocess.Popen", return_value=worker), \
             patch("bandctl.cli_client.time.monotonic", side_effect=[0, 1, 6]), \
             patch("bandctl.cli_client.time.sleep"):
            with self.assertRaisesRegex(ValueError, "did not start"):
                ensure_session("eth0", start=True)
        worker.terminate.assert_called_once()
        worker.wait.assert_called_once_with(timeout=3.0)

    def test_stop_waits_for_lock_release(self):
        lock = Mock()
        lock.acquire.side_effect = [SessionLockError("another bandctl session is already running"), None]
        with patch("bandctl.cli_client.SessionLock", return_value=lock), \
             patch("bandctl.cli_client.time.sleep"):
            wait_for_stop(timeout=3)
        self.assertEqual(lock.acquire.call_count, 2)
        lock.release.assert_called_once()
