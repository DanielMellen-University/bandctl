"""Start a real worker subprocess with fake shaping state."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bandctl.cli_client import ensure_session, wait_for_stop
from bandctl.cli_transport import request


class CliProcessTests(unittest.TestCase):
    def test_concurrent_start_waits_for_worker_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / "socket")
            lock_path = str(Path(directory) / "lock")
            waiting = Path(directory) / "waiting"
            rejected = Path(directory) / "rejected"
            script = (
                "from test_shaper import _shaper\n"
                "import bandctl.cli_worker as worker\n"
                "from bandctl.session_lock import SessionLockError\n"
                "from pathlib import Path\n"
                "import sys, time\n"
                "_, host = _shaper()\n"
                "serve = worker.serve\n"
                "def delayed(*args, **kwargs):\n"
                "    Path(sys.argv[3]).touch()\n"
                "    deadline = time.monotonic() + 4\n"
                "    while not Path(sys.argv[4]).exists() and time.monotonic() < deadline:\n"
                "        time.sleep(0.01)\n"
                "    return serve(*args, **kwargs)\n"
                "worker.serve = delayed\n"
                "try:\n"
                "    worker.run_worker('eth0', lock_path=sys.argv[1], socket_path=sys.argv[2], "
                "host=host, processes=lambda: [], detected_bps=100000000)\n"
                "except SessionLockError as exc:\n"
                "    Path(sys.argv[4]).touch()\n"
                "    print(f'bandctl: {exc}', file=sys.stderr)\n"
                "    sys.exit(1)\n"
            )
            environment = dict(os.environ)
            environment["PYTHONPATH"] = os.pathsep.join([str(Path.cwd() / "tests"), str(Path.cwd())])
            command = [sys.executable, "-c", script, lock_path, socket_path, str(waiting), str(rejected)]
            original_popen = subprocess.Popen
            children = []

            def start_fake(_args, **kwargs):
                child = original_popen(command, env=environment, **kwargs)
                children.append(child)
                return child

            try:
                first = original_popen(command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                children.append(first)
                deadline = time.monotonic() + 5
                while not waiting.exists() and first.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(waiting.exists(), "first worker did not reach socket startup")
                with patch("bandctl.cli_client.check_iface", return_value="eth0"), \
                     patch("bandctl.cli_client.subprocess.Popen", side_effect=start_fake):
                    self.assertEqual(ensure_session("eth0", start=True, socket_path=socket_path), "eth0")
                self.assertTrue(rejected.exists(), "second worker did not encounter lock contention")
                self.assertTrue(request(socket_path, {"command": "stop"})["ok"])
                wait_for_stop(lock_path=lock_path)
                self.assertEqual(first.wait(timeout=3), 0)
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                        child.wait(timeout=3)
                    if child.stderr is not None:
                        child.stderr.close()

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
