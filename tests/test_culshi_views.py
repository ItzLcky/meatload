"""Rendering a market: the embed and the button panel.

Loading the cog proves the commands are well-formed; it never builds a panel.
These do, because a market message is assembled from a database row and one
missing key would only show up when somebody opened a market.
"""

import time
import unittest

import discord

from bot.economy import lmsr, markets, views


def fake_market(**overrides):
    market = {
        "id": 7,
        "guild_id": 1,
        "creator_id": 10,
        "subject_id": 20,
        "question": "Will Bob show up to movie night?",
        "status": markets.OPEN,
        "liquidity": lmsr.liquidity_for_subsidy(1000),
        "subsidy": 1000,
        "pot": 1420,
        "q_yes": 12,
        "q_no": 4,
        "volume": 420,
        "channel_id": 99,
        "created_at": time.time(),
        "closes_at": time.time() + 86400,
        "resolved_at": None,
        "outcome": None,
        "resolver_id": None,
    }
    market.update(overrides)
    return market


class TestMarketEmbed(unittest.TestCase):
    def test_it_renders_an_open_market(self):
        embed = views.market_embed(fake_market())
        self.assertIn("#7", embed.title)
        self.assertEqual([field.name for field in embed.fields], ["YES", "NO", "Pot"])
        self.assertIn("<@20>", embed.description)  # the subject is named

    def test_it_shows_a_position_when_the_viewer_has_one(self):
        embed = views.market_embed(fake_market(), position={"yes": 5, "no": 0, "spent": 240})
        names = [field.name for field in embed.fields]
        self.assertIn("Your position", names)

    def test_it_hides_an_empty_position(self):
        embed = views.market_embed(fake_market(), position={"yes": 0, "no": 0, "spent": 0})
        self.assertNotIn("Your position", [field.name for field in embed.fields])

    def test_a_settled_market_says_so(self):
        embed = views.market_embed(fake_market(status=markets.RESOLVED, outcome=lmsr.YES))
        self.assertIn("settled **YES**", embed.description)

    def test_every_status_has_a_label(self):
        for status, outcome in [
            (markets.OPEN, None),
            (markets.CLOSED, None),
            (markets.RESOLVED, lmsr.NO),
            (markets.CANCELLED, markets.CANCELLED),
        ]:
            with self.subTest(status=status):
                self.assertTrue(views.status_label(fake_market(status=status, outcome=outcome)))

    def test_it_stays_inside_discords_embed_limits(self):
        """Long questions and a full trade list still have to fit."""
        trades = [
            {"user_id": 1, "action": "buy", "contracts": 999, "side": "yes", "cc": 12345, "price": 61}
            for _ in range(3)
        ]
        embed = views.market_embed(
            fake_market(question="Q" * 500), position={"yes": 9, "no": 9, "spent": 1}, trades=trades
        )
        self.assertLessEqual(len(embed.title), 256)
        self.assertLessEqual(len(embed), 6000)

    def test_the_odds_bar_tracks_the_price(self):
        self.assertEqual(views.odds_bar(0).count("🟩"), 0)
        self.assertEqual(views.odds_bar(100).count("🟩"), views.BAR_LENGTH)
        self.assertEqual(len(views.odds_bar(50)), views.BAR_LENGTH * 1)


class TestMarketPanel(unittest.TestCase):
    def test_an_open_market_gets_buy_and_sell_buttons(self):
        panel = views.MarketPanel(fake_market())
        labels = [item.item.label for item in panel.children]
        self.assertEqual(len(labels), 5)
        self.assertTrue(any(label.startswith("Buy YES") for label in labels))
        self.assertTrue(any(label.startswith("Buy NO") for label in labels))
        self.assertIn("Sell YES", labels)
        self.assertIn("My position", labels)

    def test_buy_buttons_quote_the_live_price(self):
        panel = views.MarketPanel(fake_market())
        market = fake_market()
        expected = lmsr.price(market["liquidity"], market["q_yes"], market["q_no"], lmsr.YES)
        self.assertIn(f"{expected} cc", panel.children[0].item.label)

    def test_a_settled_market_offers_nothing_to_trade(self):
        panel = views.MarketPanel(fake_market(status=markets.RESOLVED, outcome=lmsr.YES))
        self.assertEqual([item.item.label for item in panel.children], ["My position"])

    def test_the_market_id_travels_in_the_custom_id(self):
        """This is what makes a panel survive a restart, so it has to round-trip."""
        panel = views.MarketPanel(fake_market(id=4242))
        for item in panel.children:
            with self.subTest(custom_id=item.custom_id):
                self.assertIn("4242", item.custom_id)
                match = item.__discord_ui_compiled_template__.fullmatch(item.custom_id)
                self.assertIsNotNone(match, "a button's custom_id must match its own template")
                self.assertEqual(int(match["market"]), 4242)

    def test_the_panel_never_times_out(self):
        self.assertIsNone(views.MarketPanel(fake_market()).timeout)

    def test_it_is_a_valid_discord_payload(self):
        """What actually gets sent — catches an over-long label or a bad row."""
        panel = views.MarketPanel(fake_market())
        payload = panel.to_components()
        self.assertLessEqual(len(payload), 5)
        for item in panel.children:
            self.assertLessEqual(len(item.item.label), 80)


class TestFillEmbed(unittest.TestCase):
    def test_a_buy_reads_as_a_receipt(self):
        embed = views.fill_embed(
            {
                "market_id": 7, "side": "yes", "action": "buy", "contracts": 10,
                "cc": 540, "average": 54, "old_price": 50, "new_price": 58,
            },
            balance=9460,
        )
        self.assertIn("Bought", embed.description)
        self.assertIn("540 cc", embed.description)
        self.assertIn("50% → 58%", embed.description)

    def test_a_sell_reads_as_a_sale(self):
        embed = views.fill_embed(
            {
                "market_id": 7, "side": "no", "action": "sell", "contracts": 3,
                "cc": 120, "average": 40, "old_price": 60, "new_price": 63,
            },
            balance=100,
        )
        self.assertIn("Sold", embed.description)


if __name__ == "__main__":
    unittest.main()
