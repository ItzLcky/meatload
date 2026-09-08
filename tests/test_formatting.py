import unittest

from bot.utils.formatting import (
    format_duration,
    format_long_duration,
    humanize_list,
    parse_duration,
    progress_bar,
    truncate,
)


class TestFormatDuration(unittest.TestCase):
    def test_common_cases(self):
        self.assertEqual(format_duration(0), "0:00")
        self.assertEqual(format_duration(7), "0:07")
        self.assertEqual(format_duration(187), "3:07")
        self.assertEqual(format_duration(3727), "1:02:07")

    def test_live_streams_have_no_duration(self):
        self.assertEqual(format_duration(None), "LIVE")

    def test_negative_clamps_to_zero(self):
        self.assertEqual(format_duration(-5), "0:00")


class TestFormatLongDuration(unittest.TestCase):
    def test_units(self):
        self.assertEqual(format_long_duration(0), "0s")
        self.assertEqual(format_long_duration(45), "45s")
        self.assertEqual(format_long_duration(600), "10m")
        self.assertEqual(format_long_duration(3600), "1h")
        self.assertEqual(format_long_duration(90061), "1d 1h 1m 1s")


class TestParseDuration(unittest.TestCase):
    def test_bare_seconds(self):
        self.assertEqual(parse_duration("90"), 90)

    def test_unit_suffixes(self):
        self.assertEqual(parse_duration("10m"), 600)
        self.assertEqual(parse_duration("2h"), 7200)
        self.assertEqual(parse_duration("3d"), 259200)
        self.assertEqual(parse_duration("1w"), 604800)

    def test_compound(self):
        self.assertEqual(parse_duration("1h30m"), 5400)
        self.assertEqual(parse_duration("1h 30m 15s"), 5415)

    def test_clock_notation(self):
        self.assertEqual(parse_duration("1:30"), 90)
        self.assertEqual(parse_duration("1:02:07"), 3727)

    def test_case_insensitive(self):
        self.assertEqual(parse_duration("2H"), 7200)

    def test_rejects_nonsense(self):
        for value in ("", "   ", "soon", "10x", "abc"):
            with self.subTest(value=value):
                self.assertIsNone(parse_duration(value))


class TestProgressBar(unittest.TestCase):
    def test_marker_moves_with_position(self):
        self.assertTrue(progress_bar(0, 100).startswith("●"))
        self.assertTrue(progress_bar(100, 100).endswith("●"))
        self.assertEqual(len(progress_bar(50, 100)), 20)

    def test_live_stream(self):
        self.assertEqual(progress_bar(10, None), "🔴 LIVE")
        self.assertEqual(progress_bar(10, 0), "🔴 LIVE")

    def test_position_past_the_end_does_not_overflow(self):
        self.assertEqual(len(progress_bar(500, 100)), 20)


class TestTruncate(unittest.TestCase):
    def test_leaves_short_text_alone(self):
        self.assertEqual(truncate("hello", 10), "hello")

    def test_never_exceeds_the_limit(self):
        self.assertEqual(len(truncate("x" * 100, 10)), 10)


class TestHumanizeList(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(humanize_list([]), "")
        self.assertEqual(humanize_list(["a"]), "a")
        self.assertEqual(humanize_list(["a", "b"]), "a and b")
        self.assertEqual(humanize_list(["a", "b", "c"]), "a, b, and c")


if __name__ == "__main__":
    unittest.main()
