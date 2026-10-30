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

    def test_termination_requests_cleanup_without_interrupting_the_stack(self):
        from bandctl import cli_worker
        with patch.object(cli_worker, "_stop_signum", None):
            cli_worker._stop(15, None)
            self.assertEqual(cli_worker._stop_signum, 15)

    def test_worker_main_reports_termination_after_cleanup(self):
        from bandctl import cli_worker

        def run(_iface, *, match_wildcard, stop_requested):
            self.assertFalse(stop_requested())
            cli_worker._stop(15, None)
            self.assertTrue(stop_requested())

        with patch.object(cli_worker, "_stop_signum", None), \
             patch("bandctl.cli_worker.os.geteuid", return_value=0), \
             patch("bandctl.cli_worker.signal.signal"), \
             patch("bandctl.cli_worker.check_iface"), \
             patch("bandctl.cli_worker.run_worker", side_effect=run):
            self.assertEqual(cli_worker.main(["--iface", "eth0"]), 143)

    def test_termination_during_qdisc_change_allows_cleanup(self):
        from bandctl import cli_worker

        shaper, host = _shaper()
        run = host.run

        def terminate_after_replace(args, input_text=None):
            result = run(args, input_text)
            if args[:4] == ["tc", "qdisc", "replace", "dev"]:
                cli_worker._stop(15, None)
            return result

        with patch.object(cli_worker, "_stop_signum", None, create=True), \
             patch.object(host, "run", side_effect=terminate_after_replace):
            shaper.apply(42, None, 10, None, 100_000_000)
            self.assertEqual(cli_worker._stop_signum, 15)
            self.assertEqual(shaper.teardown(), [])
        commands = [" ".join(args) for args, _ in host.commands]
        self.assertIn("tc qdisc add dev eth0 root fq_codel", commands)
        self.assertEqual(host.files["/proc/42/cgroup"], "0::/user.slice/user-1000.slice/session-1.scope\n")
        self.assertNotIn("/run/bandctl.json", host.files)

    def test_termination_during_request_finishes_request_then_cleans_up(self):
        from bandctl import cli_worker

        _, host = _shaper()
        host.files["/proc/42/stat"] = "42 (curl) S " + "0 " * 18 + "100"
        procs = [ProcessInfo(42, "curl", "curl", "user", 0, 100)]
        run = host.run

        def terminate_after_replace(args, input_text=None):
            result = run(args, input_text)
            if args[:4] == ["tc", "qdisc", "replace", "dev"]:
                cli_worker._stop(15, None)
            return result

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(cli_worker, "_stop_signum", None), \
             patch.object(host, "run", side_effect=terminate_after_replace):
            lock_path, socket_path = str(Path(directory) / "lock"), str(Path(directory) / "socket")
            worker = threading.Thread(target=run_worker, args=("eth0",), kwargs={
                "lock_path": lock_path, "socket_path": socket_path, "host": host,
                "processes": lambda: procs, "detected_bps": 100_000_000,
                "stop_requested": lambda: cli_worker._stop_signum is not None,
            }, daemon=True)
            worker.start()
            wait_ready(socket_path)
            try:
                response = request(socket_path, {"command": "limit", "target": "42", "down": None, "up": "10"})
                self.assertTrue(response["ok"], response)
            finally:
                worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertFalse(Path(socket_path).exists())
            self.assertNotIn("/run/bandctl.json", host.files)
            self.assertEqual(host.files["/proc/42/cgroup"], "0::/user.slice/user-1000.slice/session-1.scope\n")
            self.assertIn("tc qdisc add dev eth0 root fq_codel", [" ".join(args) for args, _ in host.commands])

    def test_termination_before_serving_skips_socket_startup(self):
        _, host = _shaper()
        with tempfile.TemporaryDirectory() as directory, patch("bandctl.cli_worker.serve") as serve:
            run_worker("eth0", lock_path=str(Path(directory) / "lock"),
                       socket_path=str(Path(directory) / "socket"), host=host,
                       processes=lambda: [], detected_bps=100_000_000, stop_requested=lambda: True)
        serve.assert_not_called()

    def test_idle_worker_stops_when_termination_is_requested(self):
        _, host = _shaper()
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            lock_path, socket_path = str(Path(directory) / "lock"), str(Path(directory) / "socket")
            worker = threading.Thread(target=run_worker, args=("eth0",), kwargs={
                "lock_path": lock_path, "socket_path": socket_path, "host": host,
                "processes": lambda: [], "detected_bps": 100_000_000,
                "stop_requested": stop.is_set,
            }, daemon=True)
            worker.start()
            wait_ready(socket_path)
            stop.set()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertFalse(Path(socket_path).exists())
            with SessionLock(lock_path):
                pass

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
