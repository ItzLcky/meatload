"""Culshi: prediction markets on each other, priced in Culture Coin.

A market is one yes/no question with a deadline. Buying a contract costs the
market's current odds and pays 100 cc if you were right, so the price *is* the
server's collective guess — and it moves as people trade, which is the whole
point of doing this instead of a poll.

There is no order book to sit empty. An LMSR market maker, funded by whoever
opened the market, quotes both sides at all times; `bot/economy/lmsr.py` covers
how and why. Positions can be sold back before the deadline, so being early and
right is worth something even before the question settles.
"""

from __future__ import annotations

import logging
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..economy import bank, lmsr, markets, views
from ..utils import embeds
from ..utils.errors import FriendlyError
from ..utils.formatting import format_long_duration, parse_duration, truncate
from ..utils.paginator import send_pages

log = logging.getLogger(__name__)

STATUS_FILTERS = {"open": markets.OPEN, "closed": markets.CLOSED, "settled": markets.RESOLVED}

# Ledger kinds that add up to a trader's realised P&L.
PNL_KINDS = (bank.KIND_BUY, bank.KIND_SELL, bank.KIND_SETTLE, bank.KIND_REFUND, bank.KIND_SUBSIDY)


class Culshi(commands.Cog):
    """Prediction markets. Bet cc on what each other will do."""

    def __init__(self, bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Panels are dynamic items, so registering the classes once is enough
        # for every market message ever posted to keep working after a restart.
        self.bot.add_dynamic_items(*views.DYNAMIC_ITEMS)
        self.closer.start()

    async def cog_unload(self) -> None:
        self.closer.cancel()
        self.bot.remove_dynamic_items(*views.DYNAMIC_ITEMS)

    # ── deadlines ────────────────────────────────────────────────────────────

    @tasks.loop(seconds=60)
    async def closer(self) -> None:
        """Flip expired markets to closed and say so.

        Trading is also refused by `markets._tradeable` the moment a deadline
        passes, so this loop is about announcing the close, not enforcing it.
        """
        try:
            due = await markets.due_to_close(self.bot.db)
        except Exception:
            log.exception("Could not check for markets due to close")
            return

        for market in due:
            if not await markets.close(self.bot.db, market["id"]):
                continue
            channel = await self._announce_channel(market)
            if channel is None:
                continue
            fresh = await markets.get(self.bot.db, market["id"])
            try:
                await channel.send(
                    content=f"<@{market['creator_id']}> — time to settle this one.",
                    embed=views.market_embed(fresh or market),
                )
            except discord.HTTPException:
                log.debug("Could not announce the close of market %s", market["id"], exc_info=True)

    @closer.before_loop
    async def _before_closer(self) -> None:
        await self.bot.wait_until_ready()

    # ── trading ──────────────────────────────────────────────────────────────

    @commands.hybrid_group(
        name="culshi", aliases=["market", "markets", "predict"], fallback="list", invoke_without_command=True
    )
    @commands.guild_only()
    @app_commands.describe(status="Which markets to show. Defaults to the open ones.")
    @app_commands.choices(
        status=[
            app_commands.Choice(name="Open — trading now", value="open"),
            app_commands.Choice(name="Closed — awaiting settlement", value="closed"),
            app_commands.Choice(name="Settled", value="settled"),
        ]
    )
    async def culshi(self, ctx: commands.Context, status: str = "open") -> None:
        """List Culshi markets."""
        # The slash form is `/culshi list`, so people type `!culshi list` too;
        # read that as "just show me the markets" rather than as a bad filter.
        key = status.strip().lower()
        if key in ("list", "", "all"):
            key = "open"
        if key not in STATUS_FILTERS:
            raise FriendlyError("Show `open`, `closed`, or `settled` markets.")
        await self._show_list(ctx, STATUS_FILTERS[key])

    async def _show_list(self, ctx: commands.Context, status: str) -> None:
        rows = await markets.listing(self.bot.db, ctx.guild.id, status)
        if not rows:
            word = {markets.OPEN: "open", markets.CLOSED: "closed", markets.RESOLVED: "settled"}[status]
            raise FriendlyError(
                f"No {word} markets. `/culshi create` opens one."
                if status == markets.OPEN
                else f"No {word} markets yet."
            )

        pages: list[discord.Embed] = []
        per_page = 8
        for start in range(0, len(rows), per_page):
            lines = []
            for market in rows[start : start + per_page]:
                price = lmsr.price(market["liquidity"], market["q_yes"], market["q_no"], lmsr.YES)
                subject = f" · <@{market['subject_id']}>" if market["subject_id"] else ""
                lines.append(
                    f"`#{market['id']}` **{price}%** — {truncate(market['question'], 90)}{subject}\n"
                    f"　{views.status_label(market)} · pot {bank.format_cc(market['pot'])}"
                )
            embed = embeds.info("\n".join(lines), title="📊 Culshi markets")
            embed.set_footer(text="/culshi view <id> to trade")
            pages.append(embed)
        await send_pages(ctx, pages)

    @culshi.command(name="view", aliases=["show"])
    @app_commands.describe(market_id="The market's number, e.g. 3")
    async def culshi_view(self, ctx: commands.Context, market_id: int) -> None:
        """Open a market's trading panel."""
        market = await self._market(ctx, market_id)
        position = await markets.position(self.bot.db, market_id, ctx.author.id)
        trades = await markets.recent_trades(self.bot.db, market_id, limit=3)
        await ctx.send(
            embed=views.market_embed(market, position=position, trades=trades),
            view=views.MarketPanel(market),
        )

    @culshi.command(name="create", aliases=["open", "new"])
    @app_commands.describe(
        question="The yes/no question, e.g. `Will Sean actually stream on Friday?`",
        about="Who the market is about, if it's about someone",
        closes="How long trading stays open, e.g. `2d`, `6h`, `90m`",
        subsidy="cc you put up to seed the market maker. More = deeper market.",
    )
    async def culshi_create(
        self,
        ctx: commands.Context,
        question: str,
        about: discord.Member | None = None,
        closes: str = "7d",
        subsidy: commands.Range[int, markets.MIN_SUBSIDY, markets.MAX_SUBSIDY] = markets.DEFAULT_SUBSIDY,
    ) -> None:
        """Open a market on something someone might do.

        `/culshi create` gives you a form. With the text prefix, wrap the
        question in quotes so it doesn't get read as several arguments:
        `!culshi create "Will Sean stream on Friday?" @Sean 3d 2000`
        """
        config = await self._require_culshi(ctx)

        duration = parse_duration(closes)
        if duration is None:
            raise FriendlyError("Say how long in the form `2d`, `6h`, or `90m`.")
        if not markets.MIN_DURATION <= duration <= markets.MAX_DURATION:
            raise FriendlyError(
                f"Trading has to run between {format_long_duration(markets.MIN_DURATION)} and "
                f"{format_long_duration(markets.MAX_DURATION)}."
            )

        minimum = int(config.get("culshi_min_subsidy") or markets.MIN_SUBSIDY)
        if subsidy < minimum:
            raise FriendlyError(f"This server's markets need at least {bank.format_cc(minimum)} of seed.")

        cap = int(config.get("culshi_max_open") or 0)
        if cap and await markets.open_count(self.bot.db, ctx.guild.id) >= cap:
            raise FriendlyError(
                f"There are already {cap} open markets. Settle some before opening another."
            )
        if about is not None and about.bot:
            raise FriendlyError("Bots are far too predictable to bet on.")

        market = await markets.create(
            self.bot.db,
            ctx.guild.id,
            ctx.author.id,
            question,
            subject_id=about.id if about else None,
            duration=duration,
            subsidy=subsidy,
            channel_id=ctx.channel.id,
        )

        await ctx.send(
            content=(
                f"{ctx.author.mention} opened a market"
                + (f" about {about.mention}" if about else "")
                + " — seeded with "
                + bank.format_cc(subsidy)
                + ", and whatever the market maker doesn't lose comes back at settlement."
            ),
            embed=views.market_embed(market),
            view=views.MarketPanel(market),
        )

    @culshi.command(name="buy")
    @app_commands.describe(
        market_id="The market's number", side="yes or no", contracts="How many contracts to buy"
    )
    async def culshi_buy(
        self,
        ctx: commands.Context,
        market_id: int,
        side: Literal["yes", "no"],
        contracts: commands.Range[int, 1, lmsr.MAX_CONTRACTS] = 10,
    ) -> None:
        """Buy contracts on one side of a market."""
        await self._require_culshi(ctx)
        await self._market(ctx, market_id)
        result = await markets.buy(self.bot.db, market_id, ctx.author.id, side, contracts)
        balance = await bank.balance(self.bot.db, ctx.guild.id, ctx.author.id)
        await ctx.send(embed=views.fill_embed(result, balance))

    @culshi.command(name="sell")
    @app_commands.describe(
        market_id="The market's number", side="yes or no", contracts="How many contracts to sell"
    )
    async def culshi_sell(
        self,
        ctx: commands.Context,
        market_id: int,
        side: Literal["yes", "no"],
        contracts: commands.Range[int, 1, lmsr.MAX_CONTRACTS],
    ) -> None:
        """Sell contracts back to the market maker."""
        await self._require_culshi(ctx)
        await self._market(ctx, market_id)
        result = await markets.sell(self.bot.db, market_id, ctx.author.id, side, contracts)
        balance = await bank.balance(self.bot.db, ctx.guild.id, ctx.author.id)
        await ctx.send(embed=views.fill_embed(result, balance))

    @culshi.command(name="positions", aliases=["portfolio"])
    @app_commands.describe(member="Whose positions to show. Defaults to you.")
    async def culshi_positions(
        self, ctx: commands.Context, member: discord.Member | None = None
    ) -> None:
        """Show open positions and what they're worth."""
        member = member or ctx.author
        held = await markets.holdings(self.bot.db, ctx.guild.id, member.id)
        if not held:
            raise FriendlyError(
                f"**{member.display_name}** has no open positions. `/culshi list` to find one."
            )

        lines, invested, value = [], 0, 0
        for market in held:
            worth = lmsr.position_value(
                market["liquidity"], market["q_yes"], market["q_no"], market["yes"], market["no"]
            )
            invested += market["spent"]
            value += worth
            sides = " · ".join(
                f"**{market[side]:,}** {side.upper()}"
                for side in (lmsr.YES, lmsr.NO)
                if market[side]
            )
            price = lmsr.price(market["liquidity"], market["q_yes"], market["q_no"], lmsr.YES)
            lines.append(
                f"`#{market['id']}` {truncate(market['question'], 80)} — **{price}%**\n"
                f"　{sides} · cost {bank.format_cc(market['spent'])} · "
                f"worth {bank.format_cc(worth)} ({bank.format_cc(worth - market['spent'], sign=True)})"
            )

        embed = embeds.info("\n".join(lines), title=f"📊 Positions — {member.display_name}")
        embed.set_footer(
            text=f"Cost {invested:,} cc · value {value:,} cc · unrealised {value - invested:+,} cc"
        )
        await ctx.send(embed=embed)

    @culshi.command(name="about")
    @app_commands.describe(member="Who the markets are about")
    async def culshi_about(self, ctx: commands.Context, member: discord.Member) -> None:
        """List every market about someone."""
        rows = await markets.about(self.bot.db, ctx.guild.id, member.id)
        if not rows:
            raise FriendlyError(f"Nobody has opened a market about **{member.display_name}**. Yet.")
        lines = [
            f"`#{market['id']}` "
            f"**{lmsr.price(market['liquidity'], market['q_yes'], market['q_no'], lmsr.YES)}%** — "
            f"{truncate(market['question'], 90)}\n　{views.status_label(market)}"
            for market in rows
        ]
        await ctx.send(
            embed=embeds.info("\n".join(lines[:15]), title=f"📊 Markets about {member.display_name}")
        )

    @culshi.command(name="pnl", aliases=["traders"])
    async def culshi_pnl(self, ctx: commands.Context) -> None:
        """Who is actually good at this."""
        placeholders = ", ".join("?" for _ in PNL_KINDS)
        rows = await self.bot.db.fetchall(
            f"SELECT user_id, SUM(delta) AS pnl FROM cc_ledger"
            f" WHERE guild_id = ? AND kind IN ({placeholders})"
            f" GROUP BY user_id ORDER BY pnl DESC LIMIT 15",
            (ctx.guild.id, *PNL_KINDS),
        )
        if not rows:
            raise FriendlyError("Nobody has traded yet.")
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [
            f"{medals.get(place, f'`#{place}`')} <@{row['user_id']}> — "
            f"**{bank.format_cc(row['pnl'], sign=True)}**"
            for place, row in enumerate(rows, start=1)
        ]
        embed = embeds.info("\n".join(lines), title="📊 Culshi P&L")
        embed.set_footer(text="Settled trades only — open positions aren't counted until they pay out")
        await ctx.send(embed=embed)

    # ── settlement ───────────────────────────────────────────────────────────

    @culshi.command(name="close")
    @app_commands.describe(market_id="The market to stop trading in")
    async def culshi_close(self, ctx: commands.Context, market_id: int) -> None:
        """Stop trading early, before the deadline."""
        market = await self._market(ctx, market_id)
        await self._require_settler(ctx, market)
        if not await markets.close(self.bot.db, market_id):
            raise FriendlyError(f"Market #{market_id} isn't open.")
        await ctx.send(
            embed=embeds.success(f"Trading is closed on #{market_id}. Settle it with `/culshi resolve`.")
        )

    @culshi.command(name="resolve", aliases=["settle"])
    @app_commands.describe(
        market_id="The market to settle", outcome="What actually happened — or cancel to refund everyone"
    )
    async def culshi_resolve(
        self, ctx: commands.Context, market_id: int, outcome: Literal["yes", "no", "cancel"]
    ) -> None:
        """Settle a market and pay out."""
        market = await self._market(ctx, market_id)
        await self._require_settler(ctx, market)

        result = await markets.resolve(
            self.bot.db, market_id, markets.CANCELLED if outcome == "cancel" else outcome, ctx.author.id
        )
        settled = result["market"]

        if outcome == "cancel":
            summary = (
                f"Market #{market_id} was **cancelled**. "
                f"{bank.format_cc(result['paid'])} refunded to {len(result['payouts'])} trader(s)."
            )
        else:
            winners = (
                "\n".join(
                    f"<@{user_id}> — **{bank.format_cc(amount, sign=True)}**"
                    for user_id, amount in sorted(
                        result["payouts"].items(), key=lambda item: -item[1]
                    )[:10]
                )
                or "*Nobody held the winning side.*"
            )
            summary = (
                f"Market #{market_id} settled **{outcome.upper()}**.\n"
                f"> {truncate(settled['question'], 200)}\n\n**Paid out**\n{winners}"
            )
        if result["residue"]:
            summary += (
                f"\n\n<@{settled['creator_id']}> gets {bank.format_cc(result['residue'])} back "
                f"of the {bank.format_cc(settled['subsidy'])} seed."
            )

        embed = embeds.success(summary, title="🏁 Settled")
        await ctx.send(embed=embed)

        # Also say so where the market lives, if that isn't here.
        channel = await self._announce_channel(settled)
        if channel is not None and channel.id != ctx.channel.id:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass

    # ── configuration ────────────────────────────────────────────────────────

    @culshi.command(name="toggle")
    @app_commands.describe(state="on or off")
    @commands.has_permissions(manage_guild=True)
    async def culshi_toggle(self, ctx: commands.Context, state: Literal["on", "off"]) -> None:
        """Turn Culshi on or off."""
        await self.bot.db.set_guild_setting(ctx.guild.id, "culshi_enabled", 1 if state == "on" else 0)
        await ctx.send(embed=embeds.success(f"Culshi is now **{state}**."))

    @culshi.command(name="channel")
    @app_commands.describe(channel="Where closes and settlements get announced. Omit to use each market's own channel.")
    @commands.has_permissions(manage_guild=True)
    async def culshi_channel(
        self, ctx: commands.Context, channel: discord.TextChannel | None = None
    ) -> None:
        """Set the announcements channel."""
        await self.bot.db.set_guild_setting(
            ctx.guild.id, "culshi_channel_id", channel.id if channel else None
        )
        await ctx.send(
            embed=embeds.success(
                f"Culshi announcements go to {channel.mention}."
                if channel
                else "Culshi announcements go wherever the market was opened."
            )
        )

    @culshi.command(name="steward")
    @app_commands.describe(role="Role allowed to settle any market. Omit to remove.")
    @commands.has_permissions(manage_guild=True)
    async def culshi_steward(self, ctx: commands.Context, role: discord.Role | None = None) -> None:
        """Set a role that can settle markets it didn't open."""
        await self.bot.db.set_guild_setting(
            ctx.guild.id, "culshi_steward_role_id", role.id if role else None
        )
        await ctx.send(
            embed=embeds.success(
                f"**{role.name}** can now settle any market."
                if role
                else "Only creators and Manage Server can settle markets now."
            )
        )

    @culshi.command(name="limits")
    @app_commands.describe(
        min_subsidy="Smallest seed a market can be opened with", max_open="How many markets can be open at once"
    )
    @commands.has_permissions(manage_guild=True)
    async def culshi_limits(
        self,
        ctx: commands.Context,
        min_subsidy: commands.Range[int, markets.MIN_SUBSIDY, markets.MAX_SUBSIDY],
        max_open: commands.Range[int, 0, 500] = 25,
    ) -> None:
        """Set the seed floor and the cap on open markets."""
        await self.bot.db.set_guild_setting(ctx.guild.id, "culshi_min_subsidy", min_subsidy)
        await self.bot.db.set_guild_setting(ctx.guild.id, "culshi_max_open", max_open)
        await ctx.send(
            embed=embeds.success(
                f"Markets now need at least {bank.format_cc(min_subsidy)} of seed, and "
                + (f"at most **{max_open}** can be open at once." if max_open else "there's no cap on how many can be open.")
            )
        )

    # ── helpers ──────────────────────────────────────────────────────────────

    async def _require_culshi(self, ctx: commands.Context) -> dict:
        config = await self.bot.db.get_guild_config(ctx.guild.id)
        if not config.get("economy_enabled"):
            raise FriendlyError("Culture Coin is switched off here, so there's nothing to trade with.")
        if not config.get("culshi_enabled"):
            raise FriendlyError(
                "Culshi is switched off here. Someone with **Manage Server** can run `/culshi toggle on`."
            )
        return config

    async def _market(self, ctx: commands.Context, market_id: int) -> dict:
        market = await markets.get(self.bot.db, market_id, ctx.guild.id)
        if market is None:
            raise FriendlyError(f"There's no market #{market_id} on this server.")
        return market

    async def _require_settler(self, ctx: commands.Context, market: dict) -> None:
        """Who gets to say what happened.

        The creator settles their own market, because they wrote the question
        and know what it meant. The subject never does — a market about whether
        someone will do a thing cannot also let them grade it.
        """
        if market["subject_id"] == ctx.author.id and not ctx.author.guild_permissions.manage_guild:
            raise FriendlyError("You're what this market is about, so you don't get to settle it.")
        if ctx.author.id == market["creator_id"] or ctx.author.guild_permissions.manage_guild:
            return
        config = await self.bot.db.get_guild_config(ctx.guild.id)
        steward_id = config.get("culshi_steward_role_id")
        if steward_id and any(role.id == steward_id for role in ctx.author.roles):
            return
        raise FriendlyError(
            f"Only <@{market['creator_id']}>, a market steward, or someone with **Manage Server** "
            f"can settle #{market['id']}."
        )

    async def _announce_channel(self, market: dict) -> discord.abc.Messageable | None:
        config = await self.bot.db.get_guild_config(market["guild_id"])
        channel_id = config.get("culshi_channel_id") or market.get("channel_id")
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        return channel if isinstance(channel, discord.abc.Messageable) else None


async def setup(bot) -> None:
    await bot.add_cog(Culshi(bot))
