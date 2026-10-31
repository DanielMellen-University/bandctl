"""TCP byte counters from ss. No live sockets and no qdisc."""

from __future__ import annotations

import subprocess
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
    def test_process_name_does_not_supply_socket_identity(self):
        text = (
            'ESTAB 0 0 10.0.0.2:1 1.1.1.1:443 users:(("sk:bad",pid=5,fd=1)) ino:42 sk:a\n'
            '\t bytes_received:100 bytes_acked:20\n'
            'ESTAB 0 0 10.0.0.2:2 1.1.1.1:443 users:(("sk:bad",pid=5,fd=2)) ino:43 sk:b\n'
            '\t bytes_received:200 bytes_acked:30\n'
        )
        self.assertEqual(parse_ss_sockets(text), {
            (5, "sk:a"): (100, 20), (5, "sk:b"): (200, 30),
        })
        inode_only = ('ESTAB 0 0 10.0.0.2:3 1.1.1.1:443 '
                      'users:(("ino:99",pid=5,fd=3)) ino:44\n'
                      '\t bytes_received:50\n')
        self.assertEqual(parse_ss_sockets(inode_only), {(5, "ino:44"): (50, 0)})

    def test_process_name_does_not_add_socket_owners(self):
        text = ('ESTAB 0 0 10.0.0.2:1 1.1.1.1:443 '
                'users:(("worker,pid=99,fd=2",pid=5,fd=1)) sk:a\n'
                '\t bytes_received:100 bytes_acked:20\n')
        self.assertEqual(parse_ss(text), {5: (100, 20)})
        self.assertEqual(parse_ss_sockets(text), {(5, "sk:a"): (100, 20)})

    def test_unicode_line_separator_in_process_name_keeps_socket_owner(self):
        for separator in ("\u0085", "\u2028", "\u2029"):
            text = ('ESTAB 0 0 10.0.0.2:1 1.1.1.1:443 '
                    f'users:(("worker{separator}helper",pid=5,fd=1)) sk:a\n'
                    '\t bytes_received:100 bytes_acked:20\n')
            with self.subTest(separator=repr(separator)):
                self.assertEqual(parse_ss(text), {5: (100, 20)})
                self.assertEqual(parse_ss_sockets(text), {(5, "sk:a"): (100, 20)})
                sampler = TrafficSampler()
                sampler.rates(text, 1)
                later = text.replace("received:100", "received:110")
                self.assertEqual(sampler.rates(later, 2)[5], (80.0, 0.0))
                self.assertEqual(sampler.totals[5], (10, 0))

    def test_read_ss_requests_socket_identity(self):
        with patch("bandctl.traffic.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="")) as run:
            self.assertEqual(read_ss(), "")
        self.assertEqual(run.call_args.args[0], ["ss", "-tinpeH"])

    def test_read_ss_rejects_failed_command_with_partial_output(self):
        result = SimpleNamespace(returncode=1, stdout=_SS)
        with patch("bandctl.traffic.subprocess.run", return_value=result):
            self.assertIsNone(read_ss())

    def test_read_ss_treats_missing_command_and_timeout_as_unavailable(self):
        for error in (FileNotFoundError("ss"), subprocess.TimeoutExpired("ss", 2)):
            with self.subTest(error=error), patch("bandctl.traffic.subprocess.run", side_effect=error) as run:
                self.assertIsNone(read_ss())
                self.assertEqual(run.call_args.kwargs["timeout"], 2)

    def test_read_ss_handles_non_utf8_process_names(self):
        def run(_args, **kwargs):
            output = b'users:(("bad\xffname",pid=5,fd=1))'
            return SimpleNamespace(returncode=0, stdout=output.decode("utf-8", errors=kwargs.get("errors") or "strict"))

        with patch("bandctl.traffic.subprocess.run", side_effect=run):
            self.assertEqual(read_ss(), 'users:(("bad\ufffdname",pid=5,fd=1))')

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

    def test_ipv4_mapped_loopback_in_hex_is_excluded(self):
        text = (
            'ESTAB 0 0 [::ffff:7f00:1]:1234 [::ffff:c000:201]:443 '
            'users:(("local",pid=5,fd=1)) sk:a\n'
            '\t bytes_received:100 bytes_acked:20\n'
            'ESTAB 0 0 [::ffff:c000:201]:1234 [::ffff:c633:6401]:443 '
            'users:(("remote",pid=6,fd=1)) sk:b\n'
            '\t bytes_received:200 bytes_acked:30\n'
        )
        self.assertEqual(parse_ss(text), {6: (200, 30)})
        self.assertEqual(parse_ss_sockets(text), {(6, "sk:b"): (200, 30)})

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

    def test_missing_socket_counters_do_not_recount_previous_traffic(self):
        header = 'ESTAB 0 0 10.0.0.2:1 1.1.1.1:1 users:(("x",pid=5,fd=1)) sk:a\n'
        sampler = TrafficSampler()
        sampler.rates(header + '\t bytes_received:1000 bytes_acked:500\n', 1)
        sampler.rates(header + '\t cubic\n', 2)
        rates = sampler.rates(header + '\t bytes_received:1020 bytes_acked:510\n', 3)
        self.assertEqual(rates[5], (80.0, 40.0))
        self.assertEqual(sampler.totals[5], (20, 10))

    def test_visible_socket_keeps_baseline_during_long_counter_gap(self):
        header = 'ESTAB 0 0 10.0.0.2:1 1.1.1.1:1 users:(("x",pid=5,fd=1)) sk:a\n'
        sampler = TrafficSampler()
        sampler.rates(header + '\t bytes_received:1000 bytes_acked:500\n', 1)
        sampler.rates(header + '\t cubic\n', 30)
        sampler.rates(header + '\t cubic\n', 61)
        rates = sampler.rates(header + '\t bytes_received:1061 bytes_acked:561\n', 62)
        self.assertEqual(rates[5], (8.0, 8.0))
        self.assertEqual(sampler.totals[5], (61, 61))

    def test_unreported_socket_does_not_revive_expired_baseline(self):
        header = 'ESTAB 0 0 10.0.0.2:1 1.1.1.1:1 users:(("x",pid=5,fd=1)) sk:a\n'
        sampler = TrafficSampler()
        sampler.rates(header + '\t bytes_received:1000\n', 1)
        sampler.rates('', 61)
        sampler.rates(header + '\t cubic\n', 62)
        self.assertEqual(sampler.rates(header + '\t bytes_received:1020\n', 63)[5], (8160.0, 0.0))

    def test_first_reported_counters_after_a_gap_contribute_from_zero(self):
        header = 'ESTAB 0 0 10.0.0.2:1 1.1.1.1:1 users:(("x",pid=5,fd=1)) sk:a\n'
        sampler = TrafficSampler()
        sampler.rates(header + '\t cubic\n', 1)
        self.assertEqual(sampler.rates(header + '\t bytes_received:20\n', 2)[5], (160.0, 0.0))
        self.assertEqual(sampler.totals[5], (20, 0))

    def test_missing_one_counter_keeps_its_baseline_and_elapsed_time(self):
        header = 'ESTAB 0 0 10.0.0.2:1 1.1.1.1:1 users:(("x",pid=5,fd=1)) sk:a\n'
        for partial, expected_partial in (
            ('bytes_received:1010', (80.0, 0.0)),
            ('bytes_acked:510', (0.0, 80.0)),
        ):
            with self.subTest(partial=partial):
                sampler = TrafficSampler()
                sampler.rates(header + '\t bytes_received:1000 bytes_acked:500\n', 1)
                self.assertEqual(sampler.rates(header + '\t ' + partial + '\n', 2)[5], expected_partial)
                rates = sampler.rates(header + '\t bytes_received:1020 bytes_acked:520\n', 3)
                self.assertEqual(rates[5], (80.0, 80.0))
                self.assertEqual(sampler.totals[5], (20, 20))

    def test_switching_upload_counter_types_does_not_count_old_bytes(self):
        header = 'ESTAB 0 0 10.0.0.2:1 1.1.1.1:1 users:(("x",pid=5,fd=1)) sk:a\n'
        sampler = TrafficSampler()
        sampler.rates(header + '\t bytes_acked:100 bytes_sent:1000\n', 1)
        self.assertEqual(sampler.rates(header + '\t bytes_sent:1050\n', 2)[5], (0.0, 0.0))
        self.assertEqual(sampler.rates(header + '\t bytes_sent:1060\n', 3)[5], (0.0, 80.0))
        self.assertEqual(sampler.rates(header + '\t bytes_acked:120 bytes_sent:1070\n', 4)[5], (0.0, 0.0))
        self.assertEqual(sampler.rates(header + '\t bytes_acked:130 bytes_sent:1080\n', 5)[5], (0.0, 80.0))
        self.assertEqual(sampler.totals[5], (0, 20))

    def test_upload_counter_type_survives_missing_samples(self):
        header = 'ESTAB 0 0 10.0.0.2:1 1.1.1.1:1 users:(("x",pid=5,fd=1)) sk:a\n'
        sampler = TrafficSampler()
        sampler.rates(header + '\t bytes_acked:100\n', 1)
        sampler.rates(header + '\t bytes_received:5\n', 2)
        sampler.rates(header + '\t cubic\n', 3)
        self.assertEqual(sampler.rates(header + '\t bytes_sent:1000\n', 4)[5], (0.0, 0.0))
        self.assertEqual(sampler.rates(header + '\t bytes_sent:1010\n', 5)[5], (0.0, 80.0))
        self.assertEqual(sampler.totals[5][1], 10)

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

    def test_shared_socket_counts_traffic_for_each_owner(self):
        header = ('ESTAB 0 0 10.0.0.2:1 1.1.1.1:443 '
                  'users:(("a",pid=5,fd=1),("b",pid=6,fd=2)) sk:a\n')
        sampler = TrafficSampler()
        sampler.rates(header + '\t bytes_received:1000 bytes_acked:500\n', 1)
        rates = sampler.rates(header + '\t bytes_received:1020 bytes_acked:510\n', 3)
        self.assertEqual(rates, {5: (80.0, 40.0), 6: (80.0, 40.0)})
        self.assertEqual(sampler.totals, {5: (20, 10), 6: (20, 10)})

        one_owner = header.replace(',("b",pid=6,fd=2)', '')
        rates = sampler.rates(one_owner + '\t bytes_received:1030 bytes_acked:515\n', 4)
        self.assertEqual(rates, {5: (80.0, 40.0)})
        self.assertEqual(sampler.totals, {5: (30, 15), 6: (20, 10)})

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

    def test_expired_socket_baseline_is_not_reused_on_reappearance(self):
        socket = ('ESTAB 0 0 10.0.0.2:1 1.1.1.1:443 users:(("x",pid=5,fd=1)) sk:a\n'
                  '\t bytes_received:100 bytes_acked:20\n')
        later = socket.replace("received:100", "received:120").replace("acked:20", "acked:30")
        sampler = TrafficSampler()
        sampler.rates(socket, 1)
        sampler.rates("", 61)

        self.assertEqual(sampler.rates(later, 62)[5], (960.0, 240.0))
        self.assertEqual(sampler.totals[5], (120, 30))

    def test_long_sampling_gap_keeps_a_seen_socket_baseline(self):
        socket = ('ESTAB 0 0 10.0.0.2:1 1.1.1.1:443 users:(("x",pid=5,fd=1)) sk:a\n'
                  '\t bytes_received:100 bytes_acked:20\n')
        later = socket.replace("received:100", "received:161").replace("acked:20", "acked:81")
        sampler = TrafficSampler()
        sampler.rates(socket, 1)

        self.assertEqual(sampler.rates(later, 62)[5], (8.0, 8.0))
        self.assertEqual(sampler.totals[5], (61, 61))

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
