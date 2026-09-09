"""Wallets and markets against a real database.

The theme is money that cannot be conjured: overdrafts, double claims, double
settlements, and the conservation law that says a market only ever redistributes
cc that already existed.
"""

import asyncio
import os
import random
import tempfile
import time
import unittest

from bot.db import Database
from bot.economy import bank, lmsr, markets
from bot.utils.errors import FriendlyError

GUILD = 1
ALICE, BOB, CAROL = 10, 20, 30


class EconomyTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "test.db"))
        await self.db.connect()
        await self.db.migrate()

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def wallets(self) -> int:
        """Every cc held by a member of this guild."""
        return await bank.circulating(self.db, GUILD)

    async def supply(self) -> int:
        """Every cc in existence here: wallets plus what markets hold in escrow.

        This is the quantity that has to stay put. Looking at wallets alone
        would show cc "vanishing" the moment a market is seeded and reappearing
        when it settles.
        """
        pots = await self.db.fetchval(
            "SELECT COALESCE(SUM(pot), 0) FROM culshi_markets WHERE guild_id = ?", (GUILD,), default=0
        )
        return await self.wallets() + int(pots)

    async def fund(self, *users: int, amount: int = 10_000) -> None:
        for user in users:
            await bank.earn(self.db, GUILD, user, amount, bank.KIND_ADMIN, "test")


class TestBank(EconomyTestCase):
    async def test_a_wallet_opens_once_at_the_starting_balance(self):
        config = {"cc_start_balance": 500}
        await bank.account(self.db, GUILD, ALICE, config)
        await bank.account(self.db, GUILD, ALICE, config)
        self.assertEqual(await bank.balance(self.db, GUILD, ALICE), 500)

    async def test_the_opening_balance_is_on_the_ledger(self):
        await bank.account(self.db, GUILD, ALICE, {"cc_start_balance": 500})
        entries = await bank.history(self.db, GUILD, ALICE)
        self.assertEqual(entries[0]["delta"], 500)

    async def test_spending_more_than_you_have_is_refused(self):
        await self.fund(ALICE, amount=100)
        self.assertFalse(await bank.spend(self.db, GUILD, ALICE, 101, bank.KIND_ADMIN))
        self.assertEqual(await bank.balance(self.db, GUILD, ALICE), 100)

    async def test_concurrent_spends_cannot_overdraw_a_wallet(self):
        """The check and the deduction are one statement precisely for this."""
        await self.fund(ALICE, amount=100)
        results = await asyncio.gather(
            *(bank.spend(self.db, GUILD, ALICE, 60, bank.KIND_BUY, "race") for _ in range(5))
        )
        self.assertEqual(results.count(True), 1)
        self.assertEqual(await bank.balance(self.db, GUILD, ALICE), 40)

    async def test_a_transfer_moves_the_money_and_nothing_else(self):
        await self.fund(ALICE, amount=300)
        before = await self.wallets()
        self.assertTrue(await bank.transfer(self.db, GUILD, ALICE, BOB, 120, "gift"))
        self.assertEqual(await bank.balance(self.db, GUILD, ALICE), 180)
        self.assertEqual(await bank.balance(self.db, GUILD, BOB), 120)
        self.assertEqual(await self.wallets(), before)

    async def test_a_failed_transfer_moves_nothing(self):
        await self.fund(ALICE, amount=50)
        self.assertFalse(await bank.transfer(self.db, GUILD, ALICE, BOB, 500, "gift"))
        self.assertEqual(await bank.balance(self.db, GUILD, ALICE), 50)
        self.assertEqual(await bank.balance(self.db, GUILD, BOB), 0)

    async def test_balances_are_per_guild(self):
        await bank.earn(self.db, GUILD, ALICE, 100, bank.KIND_ADMIN)
        self.assertEqual(await bank.balance(self.db, 999, ALICE), 0)

    async def test_streaks_continue_within_the_window_and_reset_outside_it(self):
        now = time.time()
        self.assertEqual(bank.streak_after(0, 0, now), 1)
        self.assertEqual(bank.streak_after(now - 25 * 3600, 4, now), 5)
        self.assertEqual(bank.streak_after(now - 60 * 3600, 4, now), 1)

    async def test_earnings_count_income_but_not_money_moved_around(self):
        """`Earned` should say who generates cc, not who churns their own."""
        await bank.earn(self.db, GUILD, ALICE, 100, bank.KIND_WORK)
        await bank.earn(self.db, GUILD, ALICE, 900, bank.KIND_SETTLE)  # a market paid out
        await bank.earn(self.db, GUILD, ALICE, 50, bank.KIND_REFUND)
        account = await bank.account(self.db, GUILD, ALICE)
        self.assertEqual(account["balance"], 1050)
        self.assertEqual(account["lifetime_earned"], 100)

    async def test_chat_income_is_hidden_from_the_ledger_by_default(self):
        await bank.earn(self.db, GUILD, ALICE, 5, bank.KIND_CHAT)
        await bank.earn(self.db, GUILD, ALICE, 250, bank.KIND_DAILY)
        self.assertEqual(len(await bank.history(self.db, GUILD, ALICE)), 1)
        self.assertEqual(len(await bank.history(self.db, GUILD, ALICE, include_chat=True)), 2)


class TestMarkets(EconomyTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.fund(ALICE, BOB, CAROL)
        self.market = await markets.create(
            self.db, GUILD, ALICE, "Will Bob show up?", subject_id=BOB, subsidy=1000
        )

    async def test_creating_a_market_costs_the_creator_the_seed(self):
        self.assertEqual(await bank.balance(self.db, GUILD, ALICE), 9000)
        self.assertEqual(self.market["pot"], 1000)
        self.assertEqual(self.market["status"], markets.OPEN)

    async def test_a_market_cannot_be_seeded_with_money_that_is_not_there(self):
        with self.assertRaises(FriendlyError):
            await markets.create(self.db, GUILD, CAROL, "Too rich for me", subsidy=999_999)
        self.assertEqual(await bank.balance(self.db, GUILD, CAROL), 10_000)

    async def test_buying_debits_the_wallet_and_credits_the_pot(self):
        result = await markets.buy(self.db, self.market["id"], BOB, lmsr.YES, 10)
        market = await markets.get(self.db, self.market["id"])
        self.assertEqual(await bank.balance(self.db, GUILD, BOB), 10_000 - result["cc"])
        self.assertEqual(market["pot"], 1000 + result["cc"])
        self.assertEqual(market["q_yes"], 10)
        self.assertGreater(result["new_price"], result["old_price"])

    async def test_a_position_is_recorded_with_its_cost_basis(self):
        result = await markets.buy(self.db, self.market["id"], BOB, lmsr.NO, 4)
        position = await markets.position(self.db, self.market["id"], BOB)
        self.assertEqual((position["no"], position["yes"]), (4, 0))
        self.assertEqual(position["spent"], result["cc"])

    async def test_selling_what_you_do_not_hold_is_refused(self):
        with self.assertRaises(FriendlyError):
            await markets.sell(self.db, self.market["id"], BOB, lmsr.YES, 1)

    async def test_selling_returns_the_contracts_to_the_maker(self):
        await markets.buy(self.db, self.market["id"], BOB, lmsr.YES, 10)
        await markets.sell(self.db, self.market["id"], BOB, lmsr.YES, 6)
        market = await markets.get(self.db, self.market["id"])
        position = await markets.position(self.db, self.market["id"], BOB)
        self.assertEqual(market["q_yes"], 4)
        self.assertEqual(position["yes"], 4)

    async def test_a_round_trip_cannot_make_money(self):
        before = await bank.balance(self.db, GUILD, BOB)
        await markets.buy(self.db, self.market["id"], BOB, lmsr.YES, 20)
        await markets.sell(self.db, self.market["id"], BOB, lmsr.YES, 20)
        self.assertLessEqual(await bank.balance(self.db, GUILD, BOB), before)

    async def test_an_unknown_side_is_refused(self):
        with self.assertRaises(FriendlyError):
            await markets.buy(self.db, self.market["id"], BOB, "maybe", 1)

    async def test_a_closed_market_refuses_trades(self):
        self.assertTrue(await markets.close(self.db, self.market["id"]))
        with self.assertRaises(FriendlyError):
            await markets.buy(self.db, self.market["id"], BOB, lmsr.YES, 1)

    async def test_an_expired_market_refuses_trades_before_the_closer_runs(self):
        """The deadline binds immediately; the background loop only announces it."""
        await self.db.execute(
            "UPDATE culshi_markets SET closes_at = ? WHERE id = ?",
            (time.time() - 5, self.market["id"]),
        )
        with self.assertRaises(FriendlyError):
            await markets.buy(self.db, self.market["id"], BOB, lmsr.YES, 1)
        due = await markets.due_to_close(self.db)
        self.assertEqual([m["id"] for m in due], [self.market["id"]])

    async def test_concurrent_buys_all_settle_at_prices_that_actually_existed(self):
        """The per-market lock serialises them; nobody fills at a stale price."""
        await asyncio.gather(
            *(markets.buy(self.db, self.market["id"], BOB, lmsr.YES, 3) for _ in range(6))
        )
        market = await markets.get(self.db, self.market["id"])
        position = await markets.position(self.db, self.market["id"], BOB)
        self.assertEqual(market["q_yes"], 18)
        self.assertEqual(position["yes"], 18)
        self.assertEqual(market["pot"], 1000 + position["spent"])

    async def test_settling_pays_the_winners_and_returns_the_rest_to_the_creator(self):
        await markets.buy(self.db, self.market["id"], BOB, lmsr.YES, 10)
        await markets.buy(self.db, self.market["id"], CAROL, lmsr.NO, 10)
        pot = (await markets.get(self.db, self.market["id"]))["pot"]
        result = await markets.resolve(self.db, self.market["id"], lmsr.YES, ALICE)

        self.assertEqual(result["payouts"][BOB], 10 * lmsr.PAYOUT)
        self.assertNotIn(CAROL, result["payouts"])
        # Every coin in the pot goes somewhere: winners first, the rest back to
        # whoever funded the maker.
        self.assertEqual(result["paid"] + result["residue"], pot)

    async def test_a_market_only_redistributes_cc_that_already_existed(self):
        """The conservation law: settling adds nothing to the money supply."""
        before = await self.supply()
        await markets.buy(self.db, self.market["id"], BOB, lmsr.YES, 12)
        await markets.buy(self.db, self.market["id"], CAROL, lmsr.NO, 30)
        await markets.sell(self.db, self.market["id"], BOB, lmsr.YES, 5)
        await markets.resolve(self.db, self.market["id"], lmsr.NO, ALICE)
        self.assertEqual(await self.supply(), before)

    async def test_the_pot_is_emptied_by_settlement(self):
        await markets.buy(self.db, self.market["id"], BOB, lmsr.YES, 5)
        await markets.resolve(self.db, self.market["id"], lmsr.YES, ALICE)
        market = await markets.get(self.db, self.market["id"])
        self.assertEqual(market["pot"], 0)
        self.assertEqual(market["status"], markets.RESOLVED)
        self.assertEqual(market["outcome"], lmsr.YES)

    async def test_a_market_cannot_be_settled_twice(self):
        await markets.buy(self.db, self.market["id"], BOB, lmsr.YES, 5)
        await markets.resolve(self.db, self.market["id"], lmsr.YES, ALICE)
        paid_once = await bank.balance(self.db, GUILD, BOB)
        with self.assertRaises(FriendlyError):
            await markets.resolve(self.db, self.market["id"], lmsr.YES, ALICE)
        self.assertEqual(await bank.balance(self.db, GUILD, BOB), paid_once)

    async def test_racing_settlements_pay_out_exactly_once(self):
        await markets.buy(self.db, self.market["id"], BOB, lmsr.YES, 5)
        before = await self.supply()
        results = await asyncio.gather(
            *(markets.resolve(self.db, self.market["id"], lmsr.YES, ALICE) for _ in range(4)),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(await self.supply(), before)

    async def test_cancelling_refunds_what_people_paid(self):
        before_bob = await bank.balance(self.db, GUILD, BOB)
        await markets.buy(self.db, self.market["id"], BOB, lmsr.YES, 8)
        await markets.resolve(self.db, self.market["id"], markets.CANCELLED, ALICE)
        self.assertEqual(await bank.balance(self.db, GUILD, BOB), before_bob)
        # The creator's seed comes back untouched too.
        self.assertEqual(await bank.balance(self.db, GUILD, ALICE), 10_000)

    async def test_settling_something_that_never_traded_returns_the_whole_seed(self):
        await markets.resolve(self.db, self.market["id"], lmsr.NO, ALICE)
        self.assertEqual(await bank.balance(self.db, GUILD, ALICE), 10_000)

    async def test_markets_are_scoped_to_their_guild(self):
        self.assertIsNone(await markets.get(self.db, self.market["id"], guild_id=999))

    async def test_holdings_and_listings_find_the_market(self):
        await markets.buy(self.db, self.market["id"], BOB, lmsr.YES, 2)
        self.assertEqual(len(await markets.holdings(self.db, GUILD, BOB)), 1)
        self.assertEqual(len(await markets.listing(self.db, GUILD, markets.OPEN)), 1)
        self.assertEqual(len(await markets.about(self.db, GUILD, BOB)), 1)
        self.assertEqual(await markets.open_count(self.db, GUILD), 1)

    async def test_deleting_a_market_takes_its_positions_with_it(self):
        await markets.buy(self.db, self.market["id"], BOB, lmsr.YES, 2)
        await self.db.execute("DELETE FROM culshi_markets WHERE id = ?", (self.market["id"],))
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM culshi_positions"), 0)
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM culshi_trades"), 0)


class TestUpgrade(unittest.IsolatedAsyncioTestCase):
    """A server that has been running for months has to pick this up cleanly."""

    async def asyncSetUp(self):
        from bot.db import MIGRATIONS_DIR

        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "old.db"))
        await self.db.connect()

        # Rebuild a database from before the economy existed: every migration
        # up to this one applied, and a guild already configured.
        await self.db.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY, applied_at REAL NOT NULL)"
        )
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name >= "004":  # everything from the economy onwards is what we're testing
                continue
            await self.db.conn.executescript(path.read_text())
            await self.db.conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, 0)", (path.name,)
            )
        await self.db.conn.commit()
        await self.db.execute(
            "INSERT INTO guild_config (guild_id, prefix, leveling_enabled) VALUES (?, '?', 1)", (GUILD,)
        )

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_the_economy_migration_applies_to_a_live_database(self):
        await self.db.migrate()
        applied = {row["version"] for row in await self.db.fetchall("SELECT version FROM schema_migrations")}
        self.assertIn("004_economy.sql", applied)

    async def test_existing_settings_survive_and_the_new_ones_default_sensibly(self):
        await self.db.migrate()
        config = await self.db.get_guild_config(GUILD)
        self.assertEqual(config["prefix"], "?")
        self.assertEqual(config["leveling_enabled"], 1)
        self.assertEqual(config["economy_enabled"], 1)
        self.assertEqual(config["cc_start_balance"], 500)
        self.assertEqual(config["culshi_enabled"], 1)

    async def test_the_upgrade_runs_only_once(self):
        await self.db.migrate()
        await self.db.migrate()
        self.assertEqual(
            await self.db.fetchval(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = '004_economy.sql'"
            ),
            1,
        )

    async def test_trading_works_immediately_after_the_upgrade(self):
        await self.db.migrate()
        await bank.earn(self.db, GUILD, ALICE, 5000, bank.KIND_ADMIN, "test")
        market = await markets.create(self.db, GUILD, ALICE, "Does the upgrade work?", subsidy=500)
        await markets.buy(self.db, market["id"], ALICE, lmsr.YES, 3)
        self.assertEqual((await markets.get(self.db, market["id"]))["q_yes"], 3)


class TestConservation(EconomyTestCase):
    """A random week of trading, then settlement. The money supply must not move."""

    async def test_random_trading_never_changes_the_money_supply(self):
        rng = random.Random(4242)
        traders = [100, 200, 300, 400]
        await self.fund(*traders, amount=50_000)
        before = await self.supply()

        market_ids = []
        for index in range(3):
            market = await markets.create(
                self.db, GUILD, traders[index % len(traders)], f"Question {index}?",
                subsidy=rng.choice([100, 1000, 5000]),
            )
            market_ids.append(market["id"])

        for _ in range(120):
            market_id = rng.choice(market_ids)
            user = rng.choice(traders)
            side = rng.choice([lmsr.YES, lmsr.NO])
            position = await markets.position(self.db, market_id, user)
            try:
                if position[side] and rng.random() < 0.4:
                    await markets.sell(self.db, market_id, user, side, rng.randint(1, position[side]))
                else:
                    await markets.buy(self.db, market_id, user, side, rng.randint(1, 25))
            except FriendlyError:
                pass  # ran out of cc, which is allowed to happen

        for market_id in market_ids:
            await markets.resolve(
                self.db, market_id, rng.choice([lmsr.YES, lmsr.NO, markets.CANCELLED]), traders[0]
            )

        self.assertEqual(await self.supply(), before)
        self.assertEqual(
            await self.db.fetchval("SELECT COALESCE(SUM(pot), 0) FROM culshi_markets"), 0
        )

    async def test_no_wallet_ever_goes_negative(self):
        rng = random.Random(77)
        await self.fund(ALICE, amount=2000)
        market = await markets.create(self.db, GUILD, ALICE, "Broke?", subsidy=100)
        for _ in range(60):
            try:
                await markets.buy(self.db, market["id"], ALICE, rng.choice([lmsr.YES, lmsr.NO]), 20)
            except FriendlyError:
                pass
            self.assertGreaterEqual(await bank.balance(self.db, GUILD, ALICE), 0)


if __name__ == "__main__":
    unittest.main()
