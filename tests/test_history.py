"""Rate history keeps a timed window and marks missing measurements."""

import unittest

from bandctl.history import RateHistory


class HistoryTests(unittest.TestCase):
    def test_history_tracks_processes_directions_and_gaps(self):
        history = RateHistory()
        history.record(100, {1, 2}, {1: (10, 30), 2: (20, 0)})
        history.record(101, {1, 2}, {1: (20, 10)})
        history.record(102, {1, 2}, None)
        self.assertEqual(history.graph(1, 0, 102, 60)[-3:], "▄█ ")
        self.assertEqual(history.graph(1, 1, 102, 60)[-3:], "█▃ ")
        self.assertEqual(history.graph(2, 0, 102, 60)[-3:], "█▁ ")
        self.assertEqual(history.graph(None, 0, 102, 3), "   ")

    def test_window_is_bounded_and_removed_processes_are_forgotten(self):
        history = RateHistory()
        history.record(0, {1}, {1: (100, 0)})
        history.record(60, {1}, {1: (10, 0)})
        self.assertEqual(history.graph(1, 0, 60, 60), " " * 59 + "█")
        history.record(61, set(), {})
        history.record(62, {1}, {1: (0, 0)})
        self.assertEqual(history.graph(1, 0, 62, 60), " " * 59 + "▁")

    def test_narrow_graph_preserves_peaks_and_replaces_same_second(self):
        history = RateHistory()
        history.record(59, {1}, {1: (1, 0)})
        history.record(59.5, {1}, {1: (5, 0)})
        history.record(60, {1}, {1: (10, 0)})
        self.assertEqual(history.graph(1, 0, 60, 1), "█")
        self.assertEqual(history.graph(1, 0, 60, 60)[-2:], "▄█")


if __name__ == "__main__":
    unittest.main()
