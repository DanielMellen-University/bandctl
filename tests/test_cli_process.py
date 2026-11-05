"""Start a real worker subprocess with fake shaping state."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bandctl.cli_client import ensure_session, wait_for_stop
from bandctl.cli_transport import request


class CliProcessTests(unittest.TestCase):
    def test_sigterm_during_request_finishes_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path, socket_path = str(root / "lock"), str(root / "socket")
            waiting, signaled, release, result = (root / name for name in ("waiting", "signaled", "release", "result"))
            script = (
                "from test_shaper import _shaper\n"
                "from bandctl.procscan import ProcessInfo\n"
                "import bandctl.cli_worker as worker\n"
                "from pathlib import Path\n"
                "import json, sys, time\n"
                "_, host = _shaper()\n"
                "host.files['/proc/42/stat'] = '42 (curl) S ' + '0 ' * 18 + '100 0\\n'\n"
                "run = host.run\n"
                "def delayed(args, input_text=None):\n"
                "    value = run(args, input_text)\n"
                "    if args[:4] == ['tc', 'qdisc', 'replace', 'dev']:\n"
                "        Path(sys.argv[3]).touch()\n"
                "        while not Path(sys.argv[5]).exists():\n"
                "            if worker._stop_signum is not None:\n"
                "                Path(sys.argv[4]).touch()\n"
                "            time.sleep(0.01)\n"
                "    return value\n"
                "host.run = delayed\n"
                "run_worker = worker.run_worker\n"
                "def fake_worker(iface, *, match_wildcard, stop_requested):\n"
                "    return run_worker(iface, lock_path=sys.argv[1], socket_path=sys.argv[2], host=host,\n"
                "        processes=lambda: [ProcessInfo(42, 'curl', 'curl', 'user', 0, 100)],\n"
                "        detected_bps=100000000, match_wildcard=match_wildcard, stop_requested=stop_requested)\n"
                "worker.run_worker = fake_worker\n"
                "worker.check_iface = lambda iface: iface\n"
                "worker.os.geteuid = lambda: 0\n"
                "status = worker.main(['--iface', 'eth0'])\n"
                "Path(sys.argv[6]).write_text(json.dumps({'cgroup': host.files['/proc/42/cgroup'],\n"
                "    'state_exists': '/run/bandctl.json' in host.files,\n"
                "    'commands': [' '.join(args) for args, _ in host.commands]}))\n"
                "sys.exit(status)\n"
            )
            environment = dict(os.environ)
            environment["PYTHONPATH"] = os.pathsep.join([str(Path.cwd() / "tests"), str(Path.cwd())])
            child = subprocess.Popen([sys.executable, "-c", script, lock_path, socket_path,
                                      str(waiting), str(signaled), str(release), str(result)],
                                     env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            response = []

            def send_limit():
                try:
                    response.append(request(socket_path, {"command": "limit", "target": "42", "down": None, "up": "10"}, timeout=8))
                except Exception as exc:
                    response.append(exc)

            client = None
            try:
                deadline = time.monotonic() + 5
                ready = False
                while child.poll() is None and time.monotonic() < deadline:
                    try:
                        ready = request(socket_path, {"command": "ping"}, timeout=0.2)["ok"]
                    except (OSError, ValueError):
                        time.sleep(0.01)
                    if ready:
                        break
                self.assertTrue(ready, "worker did not open its socket")
                client = threading.Thread(target=send_limit)
                client.start()
                deadline = time.monotonic() + 5
                while not waiting.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(waiting.exists(), "worker did not reach the qdisc operation")
                child.send_signal(signal.SIGTERM)
                deadline = time.monotonic() + 5
                while not signaled.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(signaled.exists(), "worker did not handle SIGTERM during the operation")
                release.touch()
                client.join(timeout=8)
                self.assertFalse(client.is_alive())
                self.assertEqual(response, [{"ok": True, "result": {"pid": 42, "name": "curl", "down": None, "up": 10}}])
                self.assertEqual(child.wait(timeout=5), 143)
                outcome = json.loads(result.read_text())
                self.assertEqual(outcome["cgroup"], "0::/user.slice/user-1000.slice/session-1.scope\n")
                self.assertFalse(outcome["state_exists"])
                self.assertIn("tc qdisc add dev eth0 root fq_codel", outcome["commands"])
                self.assertFalse(Path(socket_path).exists())
                wait_for_stop(lock_path=lock_path)
            finally:
                release.touch()
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)
                if client is not None:
                    client.join(timeout=8)
                child.stderr.close()

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
                "host.files['/proc/42/stat'] = '42 (curl) S ' + '0 ' * 18 + '100 0\\n'\n"
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
