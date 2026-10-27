"""Start a real worker subprocess with fake shaping state."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bandctl.cli_client import ensure_session, wait_for_stop
from bandctl.cli_transport import request


class CliProcessTests(unittest.TestCase):
    def test_subprocess_session_starts_handles_requests_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / "socket")
            lock_path = str(Path(directory) / "lock")
            script = (
                "from test_shaper import _shaper\n"
                "from bandctl.cli_worker import run_worker\n"
                "from bandctl.procscan import ProcessInfo\n"
                "import sys\n"
                "_, host = _shaper()\n"
                "host.files['/proc/42/stat'] = '42 (curl) S ' + '0 ' * 18 + '100'\n"
                "procs = [ProcessInfo(42, 'curl', 'curl', 'user', 0, 100)]\n"
                "run_worker('eth0', lock_path=sys.argv[1], socket_path=sys.argv[2], "
                "host=host, processes=lambda: procs, detected_bps=100000000)\n"
            )
            original_popen = subprocess.Popen
            children = []

            def start_fake(_args, **kwargs):
                environment = dict(os.environ)
                environment["PYTHONPATH"] = os.pathsep.join([str(Path.cwd() / "tests"), str(Path.cwd())])
                child = original_popen([sys.executable, "-c", script, lock_path, socket_path], env=environment, **kwargs)
                children.append(child)
                return child

            try:
                with patch("bandctl.cli_client.check_iface", return_value="eth0"), \
                     patch("bandctl.cli_client.subprocess.Popen", side_effect=start_fake):
                    self.assertEqual(ensure_session("eth0", start=True, socket_path=socket_path), "eth0")
                response = request(socket_path, {"command": "limit", "target": "42", "down": "30", "up": None})
                self.assertTrue(response["ok"], response)
                self.assertTrue(request(socket_path, {"command": "stop"})["ok"])
                wait_for_stop(lock_path=lock_path)
                self.assertEqual(children[0].wait(timeout=3), 0)
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                        child.wait(timeout=3)
