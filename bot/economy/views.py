"""The market panel: one embed per market, with buttons that outlive a restart.

Buttons are `DynamicItem`s rather than a registered persistent view. The market
id lives in the custom_id, so any panel ever posted keeps working after a
restart without the bot having to remember which messages it sent — which
matters here, because a market posted on Monday is meant to be traded all week.

Anyone may press the buttons; a market with a single authorised trader would be
a strange market. Fills are answered ephemerally and the shared panel is edited
in place, so the channel sees the price move without a running commentary of
who bought what.
"""

from __future__ import annotations

import re
import time
from typing import Any

import discord

from ..utils import embeds
from ..utils.errors import FriendlyError
from ..utils.formatting import truncate
from . import bank, lmsr, markets

BAR_LENGTH = 12
SIDE_STYLE = {lmsr.YES: discord.ButtonStyle.success, lmsr.NO: discord.ButtonStyle.danger}


def odds_bar(price_yes: int, length: int = BAR_LENGTH) -> str:
    filled = round(price_yes / lmsr.PAYOUT * length)
    return "🟩" * filled + "⬛" * (length - filled)


def status_label(market: dict[str, Any]) -> str:
    status = market["status"]
    if status == markets.OPEN:
        return f"closes <t:{int(market['closes_at'])}:R>"
    if status == markets.CLOSED:
        return "closed — awaiting settlement"
    if status == markets.CANCELLED:
        return "cancelled — refunded"
    return f"settled **{(market['outcome'] or '').upper()}**"


def market_embed(
    market: dict[str, Any],
    *,
    position: dict[str, int] | None = None,
    trades: list[dict[str, Any]] | None = None,
) -> discord.Embed:
    """The canonical rendering of a market. Used by the panel and by `/culshi view`."""
    b, q_yes, q_no = market["liquidity"], market["q_yes"], market["q_no"]
    yes_price = lmsr.price(b, q_yes, q_no, lmsr.YES)
    no_price = lmsr.price(b, q_yes, q_no, lmsr.NO)
    resolved = market["status"] in (markets.RESOLVED, markets.CANCELLED)

    if market["outcome"] == lmsr.YES:
        color = embeds.GREEN
    elif market["outcome"] == lmsr.NO:
        color = embeds.RED
    elif resolved:
        color = embeds.GREY
    else:
        color = embeds.BLURPLE

    header = f"{odds_bar(yes_price)}  **{yes_price}%**\n"
    if market["subject_id"]:
        header += f"About <@{market['subject_id']}> · "
    header += f"opened by <@{market['creator_id']}> · {status_label(market)}"

    embed = discord.Embed(
        color=color,
        title=f"#{market['id']} · {truncate(market['question'], 240)}",
        description=header,
    )
    embed.add_field(name="YES", value=f"**{yes_price} {bank.CURRENCY}**\n{q_yes:,} held", inline=True)
    embed.add_field(name="NO", value=f"**{no_price} {bank.CURRENCY}**\n{q_no:,} held", inline=True)
    embed.add_field(
        name="Pot",
        value=f"{bank.format_cc(market['pot'])}\n{bank.format_cc(market['volume'])} traded",
        inline=True,
    )

    if position and (position["yes"] or position["no"]):
        value = lmsr.position_value(b, q_yes, q_no, position["yes"], position["no"])
        held = " · ".join(
            f"**{position[side]:,}** {side.upper()}" for side in (lmsr.YES, lmsr.NO) if position[side]
        )
        embed.add_field(
            name="Your position",
            value=(
                f"{held}\nCost {bank.format_cc(position['spent'])} · "
                f"now worth {bank.format_cc(value)} "
                f"({bank.format_cc(value - position['spent'], sign=True)})"
            ),
            inline=False,
        )

    if trades:
        embed.add_field(
            name="Recent trades",
            value="\n".join(
                f"<@{trade['user_id']}> {trade['action']} **{trade['contracts']:,}** "
                f"{trade['side'].upper()} for {bank.format_cc(trade['cc'])} → {trade['price']}%"
                for trade in trades
            ),
            inline=False,
        )

    embed.set_footer(
        text=f"1 contract pays {lmsr.PAYOUT} {bank.CURRENCY} if it's right · price = the market's odds"
    )
    return embed


def fill_embed(result: dict[str, Any], balance: int) -> discord.Embed:
    """The ephemeral receipt after a trade."""
    verb = "Bought" if result["action"] == "buy" else "Sold"
    return embeds.success(
        f"{verb} **{result['contracts']:,} {result['side'].upper()}** for "
        f"**{bank.format_cc(result['cc'])}** "
        f"(avg {result['average']} {bank.CURRENCY} a contract).\n"
        f"YES moved **{result['old_price']}% → {result['new_price']}%**. "
        f"Balance: {bank.format_cc(balance)}.",
        title=f"Market #{result['market_id']}",
    )


async def refresh_panel(interaction: discord.Interaction, market_id: int) -> None:
    """Re-render the panel the button lives on, after a fill moved the price."""
    if interaction.message is None:
        return
    market = await markets.get(interaction.client.db, market_id)
    if market is None:
        return
    trades = await markets.recent_trades(interaction.client.db, market_id, limit=3)
    try:
        await interaction.message.edit(embed=market_embed(market, trades=trades), view=MarketPanel(market))
    except discord.HTTPException:
        pass  # the panel is a convenience; the trade already happened


class TradeModal(discord.ui.Modal):
    """Asks for a size. The heavy lifting is `markets.buy`/`markets.sell`."""

    contracts = discord.ui.TextInput(label="Contracts", placeholder="10", max_length=6)

    def __init__(self, market_id: int, side: str, action: str, *, held: int = 0, hint: str = "") -> None:
        super().__init__(title=f"{action.title()} {side.upper()} · #{market_id}")
        self.market_id = market_id
        self.side = side
        self.action = action
        if action == "sell":
            self.contracts.default = str(held)
            self.contracts.placeholder = f"you hold {held:,}"
        elif hint:
            self.contracts.placeholder = hint

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.contracts.value.strip().replace(",", "")
        if not raw.isdigit() or int(raw) < 1:
            await interaction.response.send_message(
                embed=embeds.error("Enter a whole number of contracts."), ephemeral=True
            )
            return

        db = interaction.client.db
        amount = int(raw)
        try:
            if self.action == "buy":
                result = await markets.buy(db, self.market_id, interaction.user.id, self.side, amount)
            else:
                result = await markets.sell(db, self.market_id, interaction.user.id, self.side, amount)
        except FriendlyError as error:
            await interaction.response.send_message(embed=embeds.error(str(error)), ephemeral=True)
            return

        balance = await bank.balance(db, interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(embed=fill_embed(result, balance), ephemeral=True)
        await refresh_panel(interaction, self.market_id)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        if interaction.response.is_done():
            return
        await interaction.response.send_message(
            embed=embeds.error("That trade didn't go through. Try again in a moment."), ephemeral=True
        )


async def _guard(interaction: discord.Interaction, market_id: int) -> dict[str, Any] | None:
    """Shared button preamble: the market still exists, and is still trading."""
    market = await markets.get(interaction.client.db, market_id, interaction.guild_id)
    if market is None:
        await interaction.response.send_message(
            embed=embeds.error("That market is gone."), ephemeral=True
        )
        return None
    if market["status"] != markets.OPEN or market["closes_at"] <= time.time():
        await interaction.response.send_message(
            embed=embeds.error(f"Market #{market_id} is {status_label(market)}."), ephemeral=True
        )
        return None
    return market


class BuyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"culshi:buy:(?P<side>yes|no):(?P<market>\d+)",
):
    def __init__(self, market_id: int, side: str, price: int | None = None) -> None:
        self.market_id = market_id
        self.side = side
        label = f"Buy {side.upper()}" + (f" · {price} {bank.CURRENCY}" if price is not None else "")
        super().__init__(
            discord.ui.Button(
                label=label, style=SIDE_STYLE[side], custom_id=f"culshi:buy:{side}:{market_id}", row=0
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["market"]), match["side"])

    async def callback(self, interaction: discord.Interaction) -> None:
        market = await _guard(interaction, self.market_id)
        if market is None:
            return
        budget = await bank.balance(interaction.client.db, interaction.guild_id, interaction.user.id)
        most = lmsr.affordable(
            market["liquidity"], market["q_yes"], market["q_no"], self.side, budget
        )
        await interaction.response.send_modal(
            TradeModal(self.market_id, self.side, "buy", hint=f"you can afford {most:,}")
        )


class SellButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"culshi:sell:(?P<side>yes|no):(?P<market>\d+)",
):
    def __init__(self, market_id: int, side: str) -> None:
        self.market_id = market_id
        self.side = side
        super().__init__(
            discord.ui.Button(
                label=f"Sell {side.upper()}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"culshi:sell:{side}:{market_id}",
                row=1,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["market"]), match["side"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _guard(interaction, self.market_id) is None:
            return
        held = await markets.position(interaction.client.db, self.market_id, interaction.user.id)
        if held[self.side] < 1:
            await interaction.response.send_message(
                embed=embeds.error(f"You don't hold any {self.side.upper()} contracts here."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            TradeModal(self.market_id, self.side, "sell", held=held[self.side])
        )


class PositionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"culshi:me:(?P<market>\d+)",
):
    """Shows the presser their own position without touching the shared panel."""

    def __init__(self, market_id: int) -> None:
        self.market_id = market_id
        super().__init__(
            discord.ui.Button(
                label="My position",
                style=discord.ButtonStyle.primary,
                custom_id=f"culshi:me:{market_id}",
                row=1,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["market"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        db = interaction.client.db
        market = await markets.get(db, self.market_id, interaction.guild_id)
        if market is None:
            await interaction.response.send_message(
                embed=embeds.error("That market is gone."), ephemeral=True
            )
            return
        held = await markets.position(db, self.market_id, interaction.user.id)
        await interaction.response.send_message(
            embed=market_embed(market, position=held), ephemeral=True
        )


class MarketPanel(discord.ui.View):
    """Buy/sell buttons for one market. Persistent: the market id is in each
    custom_id, so a restart doesn't leave dead buttons behind."""

    def __init__(self, market: dict[str, Any]) -> None:
        super().__init__(timeout=None)
        b, q_yes, q_no = market["liquidity"], market["q_yes"], market["q_no"]
        if market["status"] == markets.OPEN:
            self.add_item(BuyButton(market["id"], lmsr.YES, lmsr.price(b, q_yes, q_no, lmsr.YES)))
            self.add_item(BuyButton(market["id"], lmsr.NO, lmsr.price(b, q_yes, q_no, lmsr.NO)))
            self.add_item(SellButton(market["id"], lmsr.YES))
            self.add_item(SellButton(market["id"], lmsr.NO))
        self.add_item(PositionButton(market["id"]))


DYNAMIC_ITEMS = (BuyButton, SellButton, PositionButton)
