"""Local socket transport with a fake session and temporary paths."""
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bandctl.cli_transport import _read_line, request, serve


def wait_ready(path):
    for _ in range(100):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(path)
            return
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.01)
    raise RuntimeError("socket did not become ready")


class TransportTests(unittest.TestCase):
    def test_deeply_nested_response_is_invalid(self):
        with patch("bandctl.cli_transport.socket.socket"), patch(
            "bandctl.cli_transport._read_line", return_value=b"[" * 2000 + b"]" * 2000
        ):
            with self.assertRaisesRegex(ValueError, "invalid session response"):
                request("unused", {"command": "ping"})

    def test_request_dispatch_and_stop_remove_private_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "session.sock")
            session = Mock()
            session.limit.return_value = {"pid": 42}
            thread = threading.Thread(target=serve, args=(session, path), kwargs={"interval": 0.05})
            thread.start()
            wait_ready(path)
            try:
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                result = request(path, {"command": "limit", "target": "42", "down": "30", "up": None})
                self.assertEqual(result, {"ok": True, "result": {"pid": 42}})
                time.sleep(0.1)
                self.assertTrue(session.refresh.called)
                self.assertTrue(request(path, {"command": "stop"})["ok"])
            finally:
                thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(Path(path).exists())

    def test_malformed_request_does_not_stop_server(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "session.sock")
            thread = threading.Thread(target=serve, args=(Mock(), path))
            thread.start()
            wait_ready(path)
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(path)
                    client.sendall(b"not json\n")
                    response = json.loads(client.recv(4096))
                    self.assertFalse(response["ok"])
                    self.assertIn("Expecting value", response["error"])
                self.assertTrue(request(path, {"command": "stop"})["ok"])
            finally:
                thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def test_multiple_requests_in_one_read_are_rejected(self):
        connection = Mock()
        connection.recv.return_value = b'{"command":"stop"}\n{"command":"stop"}\n'
        with self.assertRaisesRegex(ValueError, "invalid request framing"):
            _read_line(connection)

    def test_fragmented_request_is_reassembled(self):
        connection = Mock()
        connection.recv.side_effect = [b'{"command":', b'"ping"}\n']
        self.assertEqual(_read_line(connection), b'{"command":"ping"}')

    def test_session_warnings_are_returned_to_the_client(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "session.sock")
            session = Mock()
            session.iface = "eth0"
            session.warnings = ["capacity update: failed"]
            thread = threading.Thread(target=serve, args=(session, path))
            thread.start()
            wait_ready(path)
            try:
                response = request(path, {"command": "ping"})
                self.assertEqual(response["warnings"], ["capacity update: failed"])
                request(path, {"command": "stop"})
            finally:
                thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def test_deeply_nested_request_does_not_stop_server(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "session.sock")
            session = Mock()
            session.iface = "eth0"
            thread = threading.Thread(target=serve, args=(session, path), daemon=True)
            thread.start()
            wait_ready(path)
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(2)
                    client.connect(path)
                    client.sendall(b"[" * 2000 + b"]" * 2000 + b"\n")
                    response = json.loads(client.recv(4096))
                    self.assertFalse(response["ok"])
                self.assertTrue(request(path, {"command": "ping"})["ok"])
            finally:
                if thread.is_alive():
                    request(path, {"command": "stop"})
                thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def test_stop_reports_cleanup_failure_before_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "session.sock")
            cleanup = Mock(return_value=["could not restore cgroup"])
            thread = threading.Thread(target=serve, args=(Mock(), path), kwargs={"on_stop": cleanup})
            thread.start()
            wait_ready(path)
            try:
                response = request(path, {"command": "stop"})
            finally:
                thread.join(timeout=3)
            cleanup.assert_called_once()
            self.assertEqual(response, {"ok": False, "error": "could not restore cgroup"})
            self.assertFalse(thread.is_alive())

    def test_existing_file_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.sock"
            path.write_text("keep")
            with self.assertRaises(OSError):
                serve(Mock(), str(path))
            self.assertEqual(path.read_text(), "keep")
