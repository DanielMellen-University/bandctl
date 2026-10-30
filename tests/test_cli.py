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


class LimitInputTests(unittest.TestCase):
    def test_resolves_pid_and_case_insensitive_exact_name(self):
        from bandctl.cli import resolve_process
        first = ProcessInfo(42, "Firefox", "", "user", 0, 100)
        second = ProcessInfo(43, "firefox-helper", "", "user", 0, 101)
        self.assertEqual(resolve_process("42", [first, second]), first)
        self.assertEqual(resolve_process("firefox", [first, second]), first)
        with self.assertRaisesRegex(ValueError, "no process"):
            resolve_process("fox", [first, second])

    def test_refuses_ambiguous_or_unverified_processes(self):
        from bandctl.cli import resolve_process
        processes = [ProcessInfo(42, "firefox", "", "user", 0, 100),
                     ProcessInfo(43, "Firefox", "", "user", 0, 101)]
        with self.assertRaisesRegex(ValueError, "more than one"):
            resolve_process("firefox", processes)
        with self.assertRaisesRegex(ValueError, "cannot verify"):
            resolve_process("42", [ProcessInfo(42, "firefox", "", "user", 0)])
        with self.assertRaisesRegex(ValueError, "enter"):
            resolve_process("  ", processes)

    def test_percentages_match_tui_validation(self):
        from bandctl.cli import limit_percentages
        self.assertEqual(limit_percentages("30", "10%"), (30, 10))
        self.assertEqual(limit_percentages(None, "100"), (None, 100))
        for down, up in ((None, None), ("0", None), ("101", None), ("1.5", None), ("", None)):
            with self.subTest(down=down, up=up), self.assertRaises(ValueError):
                limit_percentages(down, up)


class WarningTests(unittest.TestCase):
    def test_background_warnings_are_reported_in_text_and_json(self):
        from bandctl.__main__ import main
        response = {"ok": True, "result": {"pid": 42, "down": 30, "up": None}, "warnings": ["filter failed"]}
        for json_output in (False, True):
            stdout, stderr = io.StringIO(), io.StringIO()
            args = ["limit", "42", "--down", "30"] + (["--json"] if json_output else [])
            with self.subTest(json_output=json_output), patch("os.geteuid", return_value=0), \
                 patch("bandctl.cli_client.ensure_session"), \
                 patch("bandctl.cli_transport.request", return_value=response), \
                 contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(args), 0)
            if json_output:
                self.assertEqual(json.loads(stdout.getvalue())["warnings"], ["filter failed"])
            else:
                self.assertIn("filter failed", stderr.getvalue())


    def test_failed_command_reports_background_warnings_in_text_and_json(self):
        from bandctl.__main__ import main
        response = {"ok": False, "error": "cannot remove limit", "warnings": ["flow refresh: filter failed"]}
        for json_output in (False, True):
            stdout, stderr = io.StringIO(), io.StringIO()
            args = ["remove", "42"] + (["--json"] if json_output else [])
            with self.subTest(json_output=json_output), patch("os.geteuid", return_value=0), \
                 patch("bandctl.cli_client.ensure_session"), \
                 patch("bandctl.cli_transport.request", return_value=response), \
                 contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(args), 1)
            if json_output:
                result = json.loads(stderr.getvalue())
                self.assertEqual(result["error"], "cannot remove limit")
                self.assertEqual(result["warnings"], ["flow refresh: filter failed"])
            else:
                self.assertIn("cannot remove limit", stderr.getvalue())
                self.assertIn("flow refresh: filter failed", stderr.getvalue())


class StopFailureTests(unittest.TestCase):
    def test_failed_stop_reports_the_session_error_without_waiting(self):
        from bandctl.__main__ import main
        response = {"ok": False, "error": "cleanup failed"}
        stderr = io.StringIO()
        with patch("os.geteuid", return_value=0), \
             patch("bandctl.cli_client.ensure_session"), \
             patch("bandctl.cli_client.wait_for_stop") as wait, \
             patch("bandctl.cli_transport.request", return_value=response), \
             contextlib.redirect_stderr(stderr):
            self.assertEqual(main(["stop"]), 1)
        wait.assert_not_called()
        self.assertIn("cleanup failed", stderr.getvalue())
