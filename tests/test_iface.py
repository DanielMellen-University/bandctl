"""Default-route and link-speed parsing."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bandctl.iface import (
    IfaceError,
    check_iface,
    default_iface,
    link_speed_bps,
    parse_ip_route,
    parse_proc_route,
    parse_speed_mbps,
)


class ParseTests(unittest.TestCase):
    def test_ip_route(self):
        text = "default via 192.168.1.1 dev wlan0 proto dhcp src 192.168.1.20 metric 600\n"
        self.assertEqual(parse_ip_route(text), "wlan0")
        self.assertIsNone(parse_ip_route("10.0.0.0/8 dev eth0\n"))

    def test_proc_route(self):
        text = (
            "Iface Destination Gateway Flags RefCnt Use Metric Mask\n"
            "down0 00000000 0101A8C0 0002 0 0 1 00000000\n"
            "eth1 00000000 0101A8C0 0003 0 0 600 00000000\n"
            "eth0 00000000 0101A8C0 0003 0 0 100 00000000\n"
            "other 00000000 00000000 0001 0 0 0 00FFFFFF\n"
            "bad 00000000 00000000 invalid 0 0 0 00000000\n"
        )
        self.assertEqual(parse_proc_route(text), "eth0")
        self.assertIsNone(parse_proc_route(
            "Iface Destination Gateway Flags RefCnt Use Metric Mask\n"
            "down0 00000000 0101A8C0 0002 0 0 1 00000000\n"
        ))

    def test_speed(self):
        self.assertEqual(parse_speed_mbps("1000\n"), 1000)
        self.assertIsNone(parse_speed_mbps("-1\n"))
        self.assertIsNone(parse_speed_mbps("nope"))


class LookupTests(unittest.TestCase):
    def test_ip_wins(self):
        name = default_iface(run=lambda _args: "default via 1.2.3.4 dev enp0s3\n")
        self.assertEqual(name, "enp0s3")

    def test_proc_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "route"
            path.write_text("Iface Destination Gateway Flags RefCnt Use Metric Mask\nwlan0 00000000 0 0001 0 0 10 00000000\n", encoding="utf-8")
            name = default_iface(run=lambda _args: "", proc_route=path)
        self.assertEqual(name, "wlan0")

    def test_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "route"
            path.write_text("Iface Destination\n", encoding="utf-8")
            with self.assertRaises(IfaceError):
                default_iface(run=lambda _args: "", proc_route=path)

    def test_link_speed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "speed"
            path.write_text("2500\n", encoding="utf-8")
            self.assertEqual(link_speed_bps("eth0", speed_path=path), 2_500_000_000)
            path.write_text("-1\n", encoding="utf-8")
            self.assertIsNone(link_speed_bps("eth0", speed_path=path))


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.net = Path(self.tmp.name)
        for name in ("eth0", "lo", "wlan0"):
            (self.net / name).mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_device_passes(self):
        self.assertEqual(check_iface("wlan0", self.net), "wlan0")

    def test_missing_device_lists_the_real_ones(self):
        with self.assertRaises(IfaceError) as caught:
            check_iface("eth9", self.net)
        message = str(caught.exception)
        self.assertIn("no network interface named 'eth9'", message)
        self.assertIn("eth0, lo, wlan0", message)

    def test_bad_names_are_rejected_before_the_lookup(self):
        for name in ("", ".", "..", "../eth0", "eth 0", "x" * 16):
            with self.assertRaises(IfaceError, msg=name) as caught:
                check_iface(name, self.net)
            self.assertIn("not a valid interface name", str(caught.exception))


class CommandLineTests(unittest.TestCase):
    def test_wildcard_option_reaches_tui_and_help_explains_shared_ports(self):
        import contextlib
        import io
        from unittest.mock import patch
        from bandctl.__main__ import main

        help_text = io.StringIO()
        with contextlib.redirect_stdout(help_text), self.assertRaises(SystemExit):
            main(["--help"])
        self.assertIn("--match-wildcard", help_text.getvalue())
        self.assertIn("other processes sharing that port", " ".join(help_text.getvalue().split()))
        with patch("bandctl.__main__.BandctlApp") as app, \
             patch("bandctl.__main__.signal.signal"), patch("bandctl.__main__.atexit.register"), \
             patch("os.geteuid", return_value=1000):
            app.return_value.is_root = False
            app.return_value.cleanup_errors = []
            self.assertEqual(main(["--match-wildcard"]), 0)
        app.assert_called_once_with(iface=None, startup_settings=None, match_wildcard=True)

    def test_startup_config_selects_saved_interface(self):
        from unittest.mock import patch
        from bandctl.__main__ import main
        from bandctl.settings import Settings, write_settings

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            write_settings(path, Settings("eth0", None, None, (), ()))
            with patch("bandctl.__main__.check_iface") as check, \
                 patch("bandctl.__main__.BandctlApp") as app, \
                 patch("bandctl.__main__.signal.signal"), patch("bandctl.__main__.atexit.register"):
                app.return_value.is_root = False
                app.return_value.cleanup_errors = []
                self.assertEqual(main(["--config", str(path)]), 0)
            check.assert_called_once_with("eth0")
            app.assert_called_once_with(iface="eth0", startup_settings=str(path), match_wildcard=False)

    def test_startup_config_rejects_conflicting_iface(self):
        from bandctl.__main__ import main
        from bandctl.settings import Settings, write_settings
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            write_settings(path, Settings("eth0", None, None, (), ()))
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main(["--config", str(path), "--iface", "wlan0"])
            self.assertEqual(caught.exception.code, 2)

    def test_unknown_iface_exits_with_a_usage_error(self):
        import contextlib
        import io

        from bandctl.__main__ import main

        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            main(["--iface", "no-such-dev0"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("no network interface named 'no-such-dev0'", err.getvalue())


if __name__ == "__main__":
    unittest.main()
