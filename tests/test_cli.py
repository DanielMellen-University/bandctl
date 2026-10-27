"""Read-only CLI process listing."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from bandctl.cli import list_processes
from bandctl.procscan import ProcessInfo


PROCS = [ProcessInfo(42, "curl", "curl --password secret", "alex", 2),
         ProcessInfo(3, "bash", "bash", "root", 0)]


class ListTests(unittest.TestCase):
    def test_human_and_json_output_are_sorted_and_omit_arguments(self):
        text = list_processes(processes=PROCS)
        self.assertIn("PID", text)
        self.assertLess(text.index("bash"), text.index("curl"))
        self.assertNotIn("secret", text)
        data = json.loads(list_processes(processes=PROCS, json_output=True))
        self.assertEqual([row["pid"] for row in data], [3, 42])
        self.assertEqual(set(data[0]), {"pid", "name", "user", "sockets"})

    def test_plain_output_does_not_emit_control_characters(self):
        process = ProcessInfo(4, "bad\x1bname", "", "u\nser", 0)
        text = list_processes(processes=[process])
        self.assertIn("bad?name", text)
        self.assertIn("u?ser", text)
        self.assertNotIn("\x1b", text)

    def test_list_command_does_not_start_the_tui(self):
        from bandctl.__main__ import main
        output = io.StringIO()
        with patch("bandctl.cli.scan_processes", return_value=PROCS), \
             patch("bandctl.__main__.BandctlApp") as app, contextlib.redirect_stdout(output):
            self.assertEqual(main(["list", "--json"]), 0)
        app.assert_not_called()
        self.assertEqual(len(json.loads(output.getvalue())), 2)

    def test_list_rejects_startup_config(self):
        from bandctl.__main__ import main
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["--config", "settings.json", "list"])
        self.assertEqual(caught.exception.code, 2)
