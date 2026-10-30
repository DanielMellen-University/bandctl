"""Worker lifecycle with temporary sockets and a fake shaping host."""
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bandctl.cli_transport import request
from bandctl.cli_worker import _remove_stale_socket, run_worker
from bandctl.procscan import ProcessInfo
from bandctl.session_lock import SessionLock, SessionLockError
from bandctl.shaper import ShaperError
from test_shaper import _shaper


def wait_ready(path):
    for _ in range(200):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(path)
            return
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.01)
    raise RuntimeError("worker did not become ready")


class WorkerTests(unittest.TestCase):
    def test_worker_refuses_session_when_recovery_fails(self):
        shaper, host = _shaper()
        shaper.apply(42, None, 10, None, 100_000_000)
        host.fail_substr = "tc qdisc add dev eth0 root"
        with tempfile.TemporaryDirectory() as directory:
            lock_path, socket_path = str(Path(directory) / "lock"), str(Path(directory) / "socket")
            with patch("bandctl.cli_worker.serve") as serve:
                with self.assertRaisesRegex(ShaperError, "recovery failed.*forced failure"):
                    run_worker("eth0", lock_path=lock_path, socket_path=socket_path,
                               host=host, processes=lambda: [], detected_bps=100_000_000)
            serve.assert_not_called()
            self.assertIn("/run/bandctl.json", host.files)

    def test_worker_holds_lock_and_cleans_up_after_stop(self):
        _, host = _shaper()
        host.files["/proc/42/stat"] = "42 (curl) S " + "0 " * 18 + "100"
        procs = [ProcessInfo(42, "curl", "curl", "user", 0, 100)]
        with tempfile.TemporaryDirectory() as directory:
            lock_path, socket_path = str(Path(directory) / "lock"), str(Path(directory) / "socket")
            worker = threading.Thread(target=run_worker, args=("eth0",), kwargs={
                "lock_path": lock_path, "socket_path": socket_path, "host": host,
                "processes": lambda: procs, "detected_bps": 100_000_000,
            })
            worker.start()
            wait_ready(socket_path)
            try:
                with self.assertRaises(SessionLockError):
                    SessionLock(lock_path).acquire()
                response = request(socket_path, {"command": "limit", "target": "42", "down": "30", "up": None})
                self.assertTrue(response["ok"], response)
                self.assertTrue(request(socket_path, {"command": "stop"})["ok"])
            finally:
                worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertFalse(Path(socket_path).exists())
            with SessionLock(lock_path):
                pass
            self.assertNotIn("/run/bandctl.json", host.files)

    def test_worker_cleans_up_and_unlocks_when_server_fails(self):
        from unittest.mock import patch
        _, host = _shaper()
        with tempfile.TemporaryDirectory() as directory:
            lock_path, socket_path = str(Path(directory) / "lock"), str(Path(directory) / "socket")
            with patch("bandctl.cli_worker.serve", side_effect=RuntimeError("failed")):
                with self.assertRaisesRegex(RuntimeError, "failed"):
                    run_worker("eth0", lock_path=lock_path, socket_path=socket_path,
                               host=host, processes=lambda: [], detected_bps=100_000_000)
            with SessionLock(lock_path):
                pass
            self.assertNotIn("/run/bandctl.json", host.files)

    def test_termination_requests_stack_cleanup(self):
        from bandctl.cli_worker import _stop
        with self.assertRaises(SystemExit) as caught:
            _stop(15, None)
        self.assertEqual(caught.exception.code, 143)

    def test_stale_cleanup_refuses_a_regular_file_or_live_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "socket")
            Path(path).write_text("keep")
            with self.assertRaisesRegex(RuntimeError, "not an owned socket"):
                _remove_stale_socket(path)
            self.assertEqual(Path(path).read_text(), "keep")
            Path(path).unlink()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(path)
                listener.listen()
                with self.assertRaisesRegex(RuntimeError, "already in use"):
                    _remove_stale_socket(path)
            _remove_stale_socket(path)
            self.assertFalse(Path(path).exists())
