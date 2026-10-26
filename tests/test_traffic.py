"""TCP byte counters from ss. No live sockets and no qdisc."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bandctl.traffic import TrafficSampler, above_share, parse_ss, parse_ss_sockets, read_ss

_SS = """\
ESTAB 0 0 192.168.1.97:57532 35.173.84.232:443 users:(("curl",pid=42,fd=7))
	 cubic bytes_sent:1000 bytes_acked:1000 bytes_received:5000
ESTAB 0 0 192.168.1.97:57516 35.173.84.232:443 users:(("curl",pid=42,fd=8))
	 cubic bytes_sent:2000 bytes_acked:1500 bytes_received:2500
ESTAB 0 0 [2001:db8::1]:55046 [2001:db8::2]:443 users:(("brave",pid=7,fd=3))
	 cubic bytes_sent:800 bytes_acked:800 bytes_received:100
ESTAB 0 0 127.0.0.1:5432 127.0.0.1:40000 users:(("postgres",pid=9,fd=4))
	 cubic bytes_received:999999 bytes_acked:999999
ESTAB 0 0 10.0.0.1:1 127.0.0.1:2 users:(("local",pid=3,fd=1))
	 cubic bytes_received:50 bytes_acked:50
ESTAB 0 0 192.168.1.97:9 1.2.3.4:443
	 cubic bytes_received:50 bytes_acked:50
ESTAB 0 0 192.168.1.97:10 9.9.9.9:443 users:(("old",pid=4,fd=1))
	 cubic bytes_sent:80
"""


class ParseTests(unittest.TestCase):
    def test_read_ss_requests_socket_identity(self):
        with patch("bandctl.traffic.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="")) as run:
            self.assertEqual(read_ss(), "")
        self.assertEqual(run.call_args.args[0], ["ss", "-tinpeH"])

    def test_socket_counters_use_cookie_or_inode_and_keep_zero_baselines(self):
        text = (
            'ESTAB 0 0 10.0.0.2:1 1.1.1.1:443 users:(("a",pid=5,fd=1),("b",pid=6,fd=2)) ino:42 sk:ab12\n'
            '\t bytes_received:100 bytes_acked:20\n'
            '\t unrelated detail\n'
            'ESTAB 0 0 10.0.0.2:2 1.1.1.1:443 users:(("a",pid=5,fd=3)) ino:43\n'
            '\t cubic\n'
            'ESTAB 0 0 127.0.0.1:3 127.0.0.1:4 users:(("x",pid=7,fd=4)) ino:44 sk:ab13\n'
            '\t bytes_received:100\n'
        )
        self.assertEqual(parse_ss_sockets(text), {
            (5, "sk:ab12"): (100, 20), (6, "sk:ab12"): (100, 20), (5, "ino:43"): (0, 0),
        })

    def test_socket_counters_fall_back_to_endpoints_and_bytes_sent(self):
        text = 'ESTAB 0 0 10.0.0.2:1 1.1.1.1:443 users:(("a",pid=5,fd=1))\n\t bytes_sent:30\n'
        self.assertEqual(parse_ss_sockets(text), {(5, "10.0.0.2:1 1.1.1.1:443"): (0, 30)})

    def test_sums_sockets_and_skips_loopback_and_unowned(self):
        found = parse_ss(_SS)
        self.assertEqual(found[42], (7500, 2500))
        self.assertEqual(found[7], (100, 800))
        self.assertEqual(found[4], (0, 80))
        self.assertNotIn(9, found)
        self.assertNotIn(3, found)

    def test_sampler_needs_two_samples(self):
        sampler = TrafficSampler()
        self.assertEqual(sampler.rates(_SS, 10.0), {})
        self.assertFalse(sampler.primed)
        later = _SS.replace("bytes_received:5000", "bytes_received:62500")
        rates = sampler.rates(later, 11.0)
        self.assertTrue(sampler.primed)
        # 57500 extra bytes on pid 42 in one second.
        self.assertEqual(rates[42][0], 57500 * 8)
        self.assertEqual(rates[7], (0.0, 0.0))

    def test_a_reset_counter_does_not_spike(self):
        sampler = TrafficSampler()
        before = 'ESTAB 0 0 10.0.0.2:1 1.1.1.1:1 users:(("x",pid=5,fd=1))\n\t bytes_received:1000 bytes_acked:1000\n'
        after = 'ESTAB 0 0 10.0.0.2:1 1.1.1.1:1 users:(("x",pid=5,fd=1))\n\t bytes_received:10 bytes_acked:10\n'
        sampler.rates(before, 1.0)
        self.assertEqual(sampler.rates(after, 2.0)[5], (0.0, 0.0))

    def test_closing_one_socket_does_not_erase_another_sockets_traffic(self):
        first = ('ESTAB 0 0 10.0.0.2:1 1.1.1.1:443 users:(("x",pid=5,fd=1)) sk:a\n'
                 '\t bytes_received:1000 bytes_acked:200\n')
        second = ('ESTAB 0 0 10.0.0.2:2 1.1.1.1:443 users:(("x",pid=5,fd=2)) sk:b\n'
                  '\t bytes_received:100 bytes_acked:20\n')
        sampler = TrafficSampler()
        sampler.rates(first + second, 1)
        later = second.replace("received:100", "received:150").replace("acked:20", "acked:30")
        self.assertEqual(sampler.rates(later, 2)[5], (400.0, 80.0))
        self.assertEqual(sampler.totals[5], (50, 10))

    def test_new_sockets_and_gaps_contribute_without_recounting(self):
        socket = ('ESTAB 0 0 10.0.0.2:1 1.1.1.1:443 users:(("x",pid=5,fd=1)) sk:a\n'
                  '\t bytes_received:100 bytes_acked:20\n')
        sampler = TrafficSampler()
        sampler.rates("", 1)
        self.assertEqual(sampler.rates(socket, 2)[5], (800.0, 160.0))
        sampler.rates("", 3)
        later = socket.replace("received:100", "received:120").replace("acked:20", "acked:30")
        self.assertEqual(sampler.rates(later, 4)[5], (80.0, 40.0))
        self.assertEqual(sampler.totals[5], (120, 30))
        sampler.forget_total(5)
        self.assertNotIn(5, sampler.totals)

    def test_non_increasing_clock_does_not_consume_a_sample(self):
        sampler = TrafficSampler()
        sampler.rates(_SS, 10)
        later = _SS.replace("bytes_received:5000", "bytes_received:6000")
        self.assertEqual(sampler.rates(later, 10), {})
        self.assertEqual(sampler.rates(later, 11)[42][0], 8000.0)

    def test_totals_for_idle_live_processes_are_kept_and_absent_ones_expire(self):
        sampler = TrafficSampler()
        sampler.rates(_SS, 1)
        sampler.retain_processes({42}, 200)
        sampler.retain_processes({42}, 400)
        self.assertIn(42, sampler.totals)
        self.assertNotIn(7, sampler.totals)
        sampler.retain_processes(set(), 701)
        self.assertNotIn(42, sampler.totals)

    def test_one_percent_is_not_enough(self):
        self.assertFalse(above_share(1_000_000, 0, 100_000_000, 100_000_000))
        self.assertTrue(above_share(1_000_001, 0, 100_000_000, 100_000_000))
        self.assertTrue(above_share(0, 2_000_000, 100_000_000, 100_000_000))
        self.assertFalse(above_share(5, 5, 0, 0))


if __name__ == "__main__":
    unittest.main()
