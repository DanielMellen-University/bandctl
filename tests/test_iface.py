"""Default-route and link-speed parsing."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bandctl.iface import (
    IfaceError,
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
            "Iface\tDestination\tGateway\tFlags\n"
            "eth0\t00000000\t0101A8C0\t0003\n"
            "eth0\t0001A8C0\t00000000\t0001\n"
        )
        self.assertEqual(parse_proc_route(text), "eth0")

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
            path.write_text("Iface Destination\nwlan0 00000000 0\n", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
