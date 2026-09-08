import unittest

from bot.cogs.leveling import level_from_xp, xp_for_level


def naive_xp_for_level(level: int) -> int:
    """The definition the closed form in xp_for_level is derived from."""
    return sum(5 * (i**2) + 50 * i + 100 for i in range(level))


class TestXPCurve(unittest.TestCase):
    def test_closed_form_matches_the_definition(self):
        for level in range(0, 300):
            with self.subTest(level=level):
                self.assertEqual(xp_for_level(level), naive_xp_for_level(level))

    def test_level_zero_costs_nothing(self):
        self.assertEqual(xp_for_level(0), 0)
        self.assertEqual(xp_for_level(-5), 0)

    def test_curve_is_strictly_increasing(self):
        values = [xp_for_level(level) for level in range(100)]
        self.assertEqual(values, sorted(values))
        self.assertEqual(len(set(values)), len(values))


class TestLevelFromXP(unittest.TestCase):
    def test_round_trips_against_the_curve(self):
        for level in range(0, 100):
            with self.subTest(level=level):
                threshold = xp_for_level(level)
                self.assertEqual(level_from_xp(threshold), level)
                # One XP short of the threshold is still the previous level.
                if level:
                    self.assertEqual(level_from_xp(threshold - 1), level - 1)

    def test_zero_xp_is_level_zero(self):
        self.assertEqual(level_from_xp(0), 0)

    def test_huge_xp_is_bounded(self):
        """A pathological XP value must not spin forever."""
        self.assertLessEqual(level_from_xp(10**12), 1001)


if __name__ == "__main__":
    unittest.main()
