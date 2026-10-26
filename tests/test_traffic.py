"""TCP byte counters from ss. No live sockets and no qdisc."""

from __future__ import annotations

import unittest

from bandctl.traffic import TrafficSampler, above_share, parse_ss

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

    def test_one_percent_is_not_enough(self):
        self.assertFalse(above_share(1_000_000, 0, 100_000_000, 100_000_000))
        self.assertTrue(above_share(1_000_001, 0, 100_000_000, 100_000_000))
        self.assertTrue(above_share(0, 2_000_000, 100_000_000, 100_000_000))
        self.assertFalse(above_share(5, 5, 0, 0))


if __name__ == "__main__":
    unittest.main()
