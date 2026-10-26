"""Daily schedule boundaries, with no clock or timezone changes."""

import unittest
from datetime import time, timezone

from bandctl.schedule import DailyWindow, parse_time


class ScheduleTests(unittest.TestCase):
    def test_parse_24_hour_time(self):
        self.assertEqual(parse_time(" 09:05 "), time(9, 5))
        for text in ("9:05", "24:00", "12:60", "noon", "09:05:00"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_time(text)

    def test_daytime_boundaries(self):
        window = DailyWindow(time(9), time(17))
        self.assertFalse(window.contains(time(8, 59, 59)))
        self.assertTrue(window.contains(time(9)))
        self.assertTrue(window.contains(time(16, 59, 59)))
        self.assertFalse(window.contains(time(17)))

    def test_overnight_boundaries(self):
        window = DailyWindow(time(22), time(6))
        for current in (time(22), time(23, 59), time(0), time(5, 59)):
            self.assertTrue(window.contains(current), current)
        for current in (time(6), time(12), time(21, 59)):
            self.assertFalse(window.contains(current), current)

    def test_rejects_ambiguous_or_non_minute_windows(self):
        for start, end in ((time(9), time(9)), (time(9, 0, 1), time(10)),
                           (time(9, tzinfo=timezone.utc), time(10))):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                DailyWindow(start, end)


if __name__ == "__main__":
    unittest.main()
