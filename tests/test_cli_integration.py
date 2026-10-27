"""CLI commands over a temporary socket and fake shaping host."""
import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bandctl.__main__ import main
from bandctl.cli_client import ensure_session, wait_for_stop
from bandctl.cli_worker import run_worker
from bandctl.procscan import ProcessInfo
from test_cli_worker import wait_ready
from test_shaper import _shaper


class CliIntegrationTests(unittest.TestCase):
    def test_commands_use_one_session_and_clean_up(self):
        _, host = _shaper()
        host.files["/proc/42/stat"] = "42 (curl) S " + "0 " * 18 + "100"
        processes = [ProcessInfo(42, "curl", "curl", "user", 0, 100)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path, lock_path = str(root / "socket"), str(root / "lock")
            worker = threading.Thread(target=run_worker, args=("eth0",), kwargs={
                "lock_path": lock_path, "socket_path": socket_path, "host": host,
                "processes": lambda: processes, "detected_bps": 100_000_000,
            })
            worker.start()
            wait_ready(socket_path)
            def connect(iface, *, start):
                return ensure_session(iface, start=start, socket_path=socket_path)
            output, errors = io.StringIO(), io.StringIO()
            try:
                with patch("os.geteuid", return_value=0), \
                     patch("bandctl.cli_worker.SOCKET_PATH", socket_path), \
                     patch("bandctl.cli_client.ensure_session", side_effect=connect), \
                     patch("bandctl.cli_client.wait_for_stop", side_effect=lambda: wait_for_stop(lock_path=lock_path)), \
                     contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    self.assertEqual(main(["limit", "curl", "--down", "30", "--up", "10", "--json"]), 0)
                    self.assertEqual(json.loads(output.getvalue())["pid"], 42)
                    output.seek(0); output.truncate()
                    self.assertEqual(main(["export", str(root / "settings.json")]), 0)
                    self.assertTrue((root / "settings.json").is_file())
                    self.assertEqual(main(["remove", "42"]), 0)
                    self.assertEqual(main(["import", str(root / "settings.json")]), 0)
                    self.assertEqual(main(["stop"]), 0)
            finally:
                worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors.getvalue(), "")
            self.assertNotIn("/run/bandctl.json", host.files)

    def test_invalid_import_is_rejected_before_starting_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text("bad")
            with patch("os.geteuid", return_value=0), \
                 patch("bandctl.cli_client.ensure_session") as ensure, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["import", str(path)]), 1)
            ensure.assert_not_called()

    def test_root_is_required_for_mutating_commands(self):
        with patch("os.geteuid", return_value=1000), \
             patch("bandctl.cli_client.ensure_session") as ensure, \
             contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(["limit", "42", "--down", "20"]), 1)
        self.assertIn("root is required", error.getvalue())
        ensure.assert_not_called()
