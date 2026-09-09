"""The market maker's arithmetic.

These are mostly property tests over random trade sequences rather than fixed
numbers, because the properties are what actually matter: a market must never
be a source of free cc, and its pot must always cover what it owes.
"""

import math
import random
import unittest

from bot.economy import lmsr


class TestPricing(unittest.TestCase):
    def setUp(self):
        self.b = lmsr.liquidity_for_subsidy(1000)

    def test_a_fresh_market_is_a_coin_flip(self):
        self.assertEqual(lmsr.price(self.b, 0, 0, lmsr.YES), 50)
        self.assertEqual(lmsr.price(self.b, 0, 0, lmsr.NO), 50)

    def test_the_two_sides_always_add_up_to_a_full_payout(self):
        for q_yes, q_no in [(0, 0), (3, 0), (0, 7), (12, 5), (400, 399), (9999, 1)]:
            with self.subTest(q=(q_yes, q_no)):
                yes = lmsr.price(self.b, q_yes, q_no, lmsr.YES)
                no = lmsr.price(self.b, q_yes, q_no, lmsr.NO)
                self.assertEqual(yes + no, lmsr.PAYOUT)

    def test_buying_moves_the_price_toward_the_side_bought(self):
        before = lmsr.price(self.b, 0, 0, lmsr.YES)
        after = lmsr.price(self.b, 20, 0, lmsr.YES)
        self.assertGreater(after, before)
        self.assertLess(lmsr.price(self.b, 0, 20, lmsr.YES), before)

    def test_prices_stay_on_the_board_however_lopsided_it_gets(self):
        """A runaway market still has to quote something tradeable."""
        for q in (1_000, 100_000):
            self.assertEqual(lmsr.price(self.b, q, 0, lmsr.YES), lmsr.MAX_PRICE)
            self.assertEqual(lmsr.price(self.b, q, 0, lmsr.NO), lmsr.MIN_PRICE)

    def test_the_subsidy_round_trips_to_the_liquidity_parameter(self):
        for subsidy in (100, 500, 1000, 250_000):
            b = lmsr.liquidity_for_subsidy(subsidy)
            self.assertAlmostEqual(lmsr.subsidy_for_liquidity(b), subsidy, places=6)

    def test_the_subsidy_is_exactly_the_cost_of_an_empty_market(self):
        """`pot == C(q)` is the invariant settlement leans on, and it starts here."""
        for subsidy in (100, 1000, 9999):
            b = lmsr.liquidity_for_subsidy(subsidy)
            self.assertAlmostEqual(lmsr.cost(b, 0, 0), subsidy, places=6)


class TestCosts(unittest.TestCase):
    def setUp(self):
        self.b = lmsr.liquidity_for_subsidy(1000)

    def test_a_contract_is_never_free(self):
        """Even a hopeless side costs something — it can still pay out 100."""
        self.assertGreaterEqual(lmsr.buy_cost(self.b, 100_000, 0, lmsr.NO, 1), 1)

    def test_buying_more_costs_more_per_contract(self):
        """Price impact: size pays a worse average than the quoted price."""
        one = lmsr.buy_cost(self.b, 0, 0, lmsr.YES, 1)
        fifty = lmsr.buy_cost(self.b, 0, 0, lmsr.YES, 50)
        self.assertGreater(fifty / 50, one)

    def test_an_immediate_round_trip_never_profits(self):
        """The rounding has to fall the house's way, or this is a money printer."""
        for q_yes, q_no, size in [(0, 0, 1), (0, 0, 25), (40, 10, 7), (3, 90, 1), (500, 400, 100)]:
            with self.subTest(q=(q_yes, q_no), size=size):
                cost = lmsr.buy_cost(self.b, q_yes, q_no, lmsr.YES, size)
                back = lmsr.sell_proceeds(self.b, q_yes + size, q_no, lmsr.YES, size)
                self.assertLessEqual(back, cost)

    def test_selling_pays_nothing_for_contracts_that_are_not_held(self):
        self.assertEqual(lmsr.sell_proceeds(self.b, 5, 0, lmsr.YES, 0), 0)
        # Asking to sell more than exists is clamped, not extrapolated.
        self.assertEqual(
            lmsr.sell_proceeds(self.b, 5, 0, lmsr.YES, 99),
            lmsr.sell_proceeds(self.b, 5, 0, lmsr.YES, 5),
        )

    def test_affordable_is_the_largest_order_that_fits_the_budget(self):
        for budget in (1, 50, 500, 5000, 100_000):
            with self.subTest(budget=budget):
                most = lmsr.affordable(self.b, 0, 0, lmsr.YES, budget)
                if most:
                    self.assertLessEqual(lmsr.buy_cost(self.b, 0, 0, lmsr.YES, most), budget)
                self.assertGreater(lmsr.buy_cost(self.b, 0, 0, lmsr.YES, most + 1), budget)

    def test_nothing_is_affordable_without_money(self):
        self.assertEqual(lmsr.affordable(self.b, 0, 0, lmsr.YES, 0), 0)


class TestSolvency(unittest.TestCase):
    """The property the whole design rests on: a market cannot pay out more cc
    than it holds, whatever sequence of trades it sees."""

    def test_the_pot_always_covers_the_winning_side(self):
        rng = random.Random(20260908)
        for trial in range(200):
            subsidy = rng.choice([100, 500, 1000, 25_000])
            b = lmsr.liquidity_for_subsidy(subsidy)
            q_yes = q_no = 0
            pot = subsidy

            for _ in range(rng.randint(1, 40)):
                side = rng.choice([lmsr.YES, lmsr.NO])
                held = q_yes if side == lmsr.YES else q_no
                if held and rng.random() < 0.35:
                    size = rng.randint(1, held)
                    pot -= lmsr.sell_proceeds(b, q_yes, q_no, side, size)
                    q_yes -= size if side == lmsr.YES else 0
                    q_no -= size if side == lmsr.NO else 0
                else:
                    size = rng.randint(1, 60)
                    pot += lmsr.buy_cost(b, q_yes, q_no, side, size)
                    q_yes += size if side == lmsr.YES else 0
                    q_no += size if side == lmsr.NO else 0

            with self.subTest(trial=trial, q=(q_yes, q_no)):
                self.assertGreaterEqual(pot, max(q_yes, q_no) * lmsr.PAYOUT)
                self.assertGreaterEqual(pot, 0)

    def test_the_makers_loss_never_exceeds_the_subsidy(self):
        """What the maker can lose is exactly what the creator put up — that is
        the deal, and it is why the seed is priced the way it is."""
        rng = random.Random(11)
        for subsidy in (100, 1000, 50_000):
            b = lmsr.liquidity_for_subsidy(subsidy)
            for _ in range(50):
                q_yes = rng.randint(0, 500)
                q_no = rng.randint(0, 500)
                collected = lmsr.cost(b, q_yes, q_no) - lmsr.cost(b, 0, 0)
                worst_payout = max(q_yes, q_no) * lmsr.PAYOUT
                loss = worst_payout - collected
                self.assertLessEqual(loss, subsidy + 1e-6)


class TestPositions(unittest.TestCase):
    def test_a_position_is_marked_at_the_current_price(self):
        b = lmsr.liquidity_for_subsidy(1000)
        value = lmsr.position_value(b, 10, 0, 10, 0)
        self.assertEqual(value, 10 * lmsr.price(b, 10, 0, lmsr.YES))

    def test_settlement_pays_the_winning_side_only(self):
        self.assertEqual(lmsr.payout_for(lmsr.YES, 7, 3), 700)
        self.assertEqual(lmsr.payout_for(lmsr.NO, 7, 3), 300)
        self.assertEqual(lmsr.payout_for("cancelled", 7, 3), 0)


if __name__ == "__main__":
    unittest.main()
