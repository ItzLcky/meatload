"""Culture Coin (cc): wallets, income, and transfers.

cc is per-server. Members earn it by turning up — chatting, a daily claim, an
hourly job — and spend it on Culshi markets, which is where it gets interesting.
Everything that moves cc goes through `bot/economy/bank.py`, so the rules about
overdrafts and the ledger live in one place rather than in each command.
"""

from __future__ import annotations

import random
import time
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from ..economy import bank, lmsr, markets
from ..utils import embeds
from ..utils.errors import FriendlyError
from ..utils.formatting import format_long_duration
from ..utils.paginator import send_pages
from ..utils.views import Confirm

JOBS = [
    "moderated a flame war in #general",
    "explained the joke to someone in DMs",
    "made a playlist nobody asked for",
    "took the minutes at movie night",
    "found the good emoji pack",
    "wrote patch notes nobody read",
    "settled an argument about pineapple",
    "carried a ranked game",
    "fixed the printer, somehow",
    "ran the numbers on a terrible idea",
    "curated the memes",
    "translated a voice note",
]

LEDGER_LABELS = {
    bank.KIND_CHAT: "💬 chatting",
    bank.KIND_DAILY: "📅 daily",
    bank.KIND_WORK: "🛠️ work",
    bank.KIND_PAY_IN: "📥 received",
    bank.KIND_PAY_OUT: "📤 sent",
    bank.KIND_ADMIN: "⚙️ adjustment",
    bank.KIND_BUY: "📈 culshi buy",
    bank.KIND_SELL: "📉 culshi sell",
    bank.KIND_SUBSIDY: "🌱 market seed",
    bank.KIND_SETTLE: "🏁 settlement",
    bank.KIND_REFUND: "↩️ refund",
}


class Economy(commands.Cog):
    """Culture Coin — earn it, send it, check who's hoarding it."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # ── passive income ───────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Pay a little cc for chatting, on the same cooldown shape as XP.

        The cooldown is enforced by the `last_chat_at <= ?` guard on the UPDATE
        rather than by reading the row first, so a burst of messages can only
        ever be paid once.
        """
        if message.author.bot or message.guild is None or not message.content:
            return

        config = await self.bot.db.get_guild_config(message.guild.id)
        if not config.get("economy_enabled"):
            return

        ignored = await self.bot.db.fetchone(
            "SELECT 1 FROM economy_ignores WHERE guild_id = ? AND channel_id = ?",
            (message.guild.id, message.channel.id),
        )
        if ignored is not None:
            return

        low = config.get("cc_chat_min") or 0
        high = max(low, config.get("cc_chat_max") or 0)
        if high <= 0:
            return

        now = time.time()
        cutoff = now - (config.get("cc_chat_cooldown") or 60)
        amount = random.randint(low, high)

        await bank.ensure_account(
            self.bot.db, message.guild.id, message.author.id, int(config.get("cc_start_balance") or 0)
        )
        cursor = await self.bot.db.execute(
            "UPDATE balances SET balance = balance + ?, lifetime_earned = lifetime_earned + ?,"
            " last_chat_at = ? WHERE guild_id = ? AND user_id = ? AND last_chat_at <= ?",
            (amount, amount, now, message.guild.id, message.author.id, cutoff),
        )
        if cursor.rowcount == 1:
            await self.bot.db.execute(
                "INSERT INTO cc_ledger (guild_id, user_id, delta, balance, kind, note, created_at)"
                " SELECT guild_id, user_id, ?, balance, ?, NULL, ? FROM balances"
                " WHERE guild_id = ? AND user_id = ?",
                (amount, bank.KIND_CHAT, now, message.guild.id, message.author.id),
            )

    # ── wallet ───────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="balance", aliases=["bal", "wallet", "coins"])
    @app_commands.describe(member="Whose wallet to check. Defaults to you.")
    @commands.guild_only()
    async def balance(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        """Check a Culture Coin balance."""
        member = member or ctx.author
        config = await self.bot.db.get_guild_config(ctx.guild.id)
        account = await bank.account(self.bot.db, ctx.guild.id, member.id, config)
        place = await bank.rank_of(self.bot.db, ctx.guild.id, member.id)

        held = await markets.holdings(self.bot.db, ctx.guild.id, member.id)
        tied_up = sum(
            lmsr.position_value(m["liquidity"], m["q_yes"], m["q_no"], m["yes"], m["no"]) for m in held
        )

        embed = embeds.info(
            f"{bank.COIN} **{bank.format_cc(account['balance'])}**", title=f"{member.display_name}'s wallet"
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Rank", value=f"**#{place}**", inline=True)
        embed.add_field(name="Earned", value=bank.format_cc(account["lifetime_earned"]), inline=True)
        embed.add_field(name="Daily streak", value=f"{account['daily_streak']} day(s)", inline=True)
        if held:
            embed.add_field(
                name="In Culshi markets",
                value=(
                    f"{bank.format_cc(tied_up)} across {len(held)} position(s)\n"
                    f"Net worth: **{bank.format_cc(account['balance'] + tied_up)}**"
                ),
                inline=False,
            )
        if member == ctx.author:
            embed.set_footer(text="/daily and /work top you up · /culshi list to put it to work")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="pay", aliases=["transfer"])
    @app_commands.describe(member="Who to pay", amount="How much cc to send")
    @commands.guild_only()
    async def pay(
        self, ctx: commands.Context, member: discord.Member, amount: commands.Range[int, 1, 10_000_000]
    ) -> None:
        """Send someone Culture Coin."""
        await self._require_economy(ctx)
        if member.bot:
            raise FriendlyError("Bots have no use for cc.")
        if member.id == ctx.author.id:
            raise FriendlyError("Moving cc between your own pockets achieves little.")

        note = f"{ctx.author.display_name} → {member.display_name}"
        if not await bank.transfer(self.bot.db, ctx.guild.id, ctx.author.id, member.id, amount, note):
            held = await bank.balance(self.bot.db, ctx.guild.id, ctx.author.id)
            raise FriendlyError(
                f"You have {bank.format_cc(held)} and tried to send {bank.format_cc(amount)}."
            )

        remaining = await bank.balance(self.bot.db, ctx.guild.id, ctx.author.id)
        await ctx.send(
            embed=embeds.success(
                f"Sent **{bank.format_cc(amount)}** to {member.mention}. "
                f"You have {bank.format_cc(remaining)} left."
            )
        )

    @commands.hybrid_command(name="daily")
    @commands.guild_only()
    async def daily(self, ctx: commands.Context) -> None:
        """Claim your daily Culture Coin, with a streak bonus."""
        await self._require_economy(ctx)
        config = await self.bot.db.get_guild_config(ctx.guild.id)
        account = await bank.account(self.bot.db, ctx.guild.id, ctx.author.id, config)

        now = time.time()
        elapsed = now - account["last_daily_at"]
        if elapsed < bank.DAILY_COOLDOWN:
            raise FriendlyError(
                f"Already claimed. Next one in **{format_long_duration(bank.DAILY_COOLDOWN - elapsed)}**."
            )

        streak = bank.streak_after(account["last_daily_at"], account["daily_streak"], now)
        base = int(config.get("cc_daily_amount") or 0)
        # The bonus grows with the streak but stops at double, so a long streak
        # is worth keeping without turning into the only thing that matters.
        bonus = min(base, (streak - 1) * int(config.get("cc_daily_streak_bonus") or 0))
        total = base + bonus

        # Stamp the claim before paying: if this update wins, the payout is ours
        # to make, and a double `/daily` cannot both get through.
        cursor = await self.bot.db.execute(
            "UPDATE balances SET last_daily_at = ?, daily_streak = ?"
            " WHERE guild_id = ? AND user_id = ? AND last_daily_at = ?",
            (now, streak, ctx.guild.id, ctx.author.id, account["last_daily_at"]),
        )
        if cursor.rowcount != 1:
            raise FriendlyError("That claim just went through somewhere else.")

        balance = await bank.earn(
            self.bot.db, ctx.guild.id, ctx.author.id, total, bank.KIND_DAILY, f"Day {streak}"
        )
        text = f"Claimed **{bank.format_cc(total)}**."
        if bonus:
            text += f" ({bank.format_cc(base)} + {bank.format_cc(bonus)} streak bonus)"
        await ctx.send(
            embed=embeds.success(
                f"{text}\n🔥 **{streak} day** streak · balance {bank.format_cc(balance)}",
                title="Daily",
            )
        )

    @commands.hybrid_command(name="work")
    @commands.guild_only()
    async def work(self, ctx: commands.Context) -> None:
        """Do an odd job for Culture Coin."""
        await self._require_economy(ctx)
        config = await self.bot.db.get_guild_config(ctx.guild.id)
        account = await bank.account(self.bot.db, ctx.guild.id, ctx.author.id, config)

        now = time.time()
        cooldown = int(config.get("cc_work_cooldown") or 3600)
        elapsed = now - account["last_work_at"]
        if elapsed < cooldown:
            raise FriendlyError(
                f"You've done enough for now. Back to it in **{format_long_duration(cooldown - elapsed)}**."
            )

        cursor = await self.bot.db.execute(
            "UPDATE balances SET last_work_at = ? WHERE guild_id = ? AND user_id = ? AND last_work_at = ?",
            (now, ctx.guild.id, ctx.author.id, account["last_work_at"]),
        )
        if cursor.rowcount != 1:
            raise FriendlyError("You're already on the clock.")

        low = int(config.get("cc_work_min") or 0)
        high = max(low, int(config.get("cc_work_max") or 0))
        pay = random.randint(low, high)
        job = random.choice(JOBS)
        balance = await bank.earn(self.bot.db, ctx.guild.id, ctx.author.id, pay, bank.KIND_WORK, job)
        await ctx.send(
            embed=embeds.success(
                f"You {job} and got **{bank.format_cc(pay)}**.\nBalance: {bank.format_cc(balance)}",
                title="Work",
            )
        )

    @commands.hybrid_command(name="richest", aliases=["baltop", "rich"])
    @commands.guild_only()
    async def richest(self, ctx: commands.Context) -> None:
        """Who is holding the most Culture Coin."""
        rows = await bank.richest(self.bot.db, ctx.guild.id)
        if not rows:
            raise FriendlyError("Nobody has any cc yet. `/daily` starts the economy.")

        supply = await bank.circulating(self.bot.db, ctx.guild.id)
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        pages: list[discord.Embed] = []
        per_page = 10
        for start in range(0, len(rows), per_page):
            lines = []
            for offset, row in enumerate(rows[start : start + per_page]):
                place = start + offset + 1
                prefix = medals.get(place, f"`#{place}`")
                lines.append(f"{prefix} <@{row['user_id']}> — **{bank.format_cc(row['balance'])}**")
            embed = embeds.info("\n".join(lines), title=f"{bank.COIN} Richest in {ctx.guild.name}")
            embed.set_footer(text=f"{bank.format_cc(supply)} in circulation")
            pages.append(embed)
        await send_pages(ctx, pages)

    # ── configuration ────────────────────────────────────────────────────────

    @commands.hybrid_group(name="economy", aliases=["eco"], fallback="show", invoke_without_command=True)
    @commands.guild_only()
    async def economy(self, ctx: commands.Context) -> None:
        """Show how the economy is configured."""
        config = await self.bot.db.get_guild_config(ctx.guild.id)
        supply = await bank.circulating(self.bot.db, ctx.guild.id)
        wallets = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM balances WHERE guild_id = ?", (ctx.guild.id,), default=0
        )
        ignores = await self.bot.db.fetchall(
            "SELECT channel_id FROM economy_ignores WHERE guild_id = ?", (ctx.guild.id,)
        )

        embed = embeds.info(
            f"**Enabled:** {'yes' if config.get('economy_enabled') else 'no'}\n"
            f"**Starting balance:** {bank.format_cc(config.get('cc_start_balance') or 0)}\n"
            f"**Chatting:** {config.get('cc_chat_min')}–{config.get('cc_chat_max')} cc every "
            f"{format_long_duration(config.get('cc_chat_cooldown') or 60)}\n"
            f"**Daily:** {bank.format_cc(config.get('cc_daily_amount') or 0)} "
            f"+ {bank.format_cc(config.get('cc_daily_streak_bonus') or 0)} a day of streak\n"
            f"**Work:** {config.get('cc_work_min')}–{config.get('cc_work_max')} cc every "
            f"{format_long_duration(config.get('cc_work_cooldown') or 3600)}",
            title=f"{bank.COIN} {bank.CURRENCY_NAME}",
        )
        embed.add_field(
            name="Supply", value=f"{bank.format_cc(supply)} across {wallets} wallet(s)", inline=False
        )
        if ignores:
            embed.add_field(
                name="No cc for chatting in",
                value=" ".join(f"<#{row['channel_id']}>" for row in ignores),
                inline=False,
            )
        await ctx.send(embed=embed)

    @economy.command(name="history")
    @app_commands.describe(member="Whose ledger to read. Defaults to you.", chat="Include chat income")
    async def economy_history(
        self, ctx: commands.Context, member: discord.Member | None = None, chat: bool = False
    ) -> None:
        """Read recent cc movements."""
        member = member or ctx.author
        if member != ctx.author and not ctx.author.guild_permissions.manage_guild:
            raise FriendlyError("You need **Manage Server** to read someone else's ledger.")

        rows = await bank.history(self.bot.db, ctx.guild.id, member.id, 20, include_chat=chat)
        if not rows:
            raise FriendlyError(f"Nothing on **{member.display_name}**'s ledger yet.")

        lines = [
            f"`{row['delta']:+,}` {LEDGER_LABELS.get(row['kind'], row['kind'])}"
            + (f" — {row['note']}" if row["note"] else "")
            + f" <t:{int(row['created_at'])}:R>"
            for row in rows
        ]
        embed = embeds.info("\n".join(lines), title=f"Ledger — {member.display_name}")
        if not chat:
            embed.set_footer(text="Chat income hidden · add chat:True to see it")
        await ctx.send(embed=embed)

    @economy.command(name="toggle")
    @app_commands.describe(state="on or off")
    @commands.has_permissions(manage_guild=True)
    async def economy_toggle(self, ctx: commands.Context, state: Literal["on", "off"]) -> None:
        """Turn the currency on or off."""
        await self.bot.db.set_guild_setting(ctx.guild.id, "economy_enabled", 1 if state == "on" else 0)
        await ctx.send(embed=embeds.success(f"Culture Coin is now **{state}**."))

    @economy.command(name="give")
    @app_commands.describe(member="Who to pay", amount="How much cc to mint")
    @commands.has_permissions(manage_guild=True)
    async def economy_give(
        self, ctx: commands.Context, member: discord.Member, amount: commands.Range[int, 1, 10_000_000]
    ) -> None:
        """Mint cc into someone's wallet."""
        balance = await bank.earn(
            self.bot.db, ctx.guild.id, member.id, amount, bank.KIND_ADMIN, f"Granted by {ctx.author}"
        )
        await ctx.send(
            embed=embeds.success(
                f"Gave **{member.display_name}** {bank.format_cc(amount)} "
                f"(now {bank.format_cc(balance)})."
            )
        )

    @economy.command(name="take")
    @app_commands.describe(member="Whose wallet to debit", amount="How much cc to remove")
    @commands.has_permissions(manage_guild=True)
    async def economy_take(
        self, ctx: commands.Context, member: discord.Member, amount: commands.Range[int, 1, 10_000_000]
    ) -> None:
        """Remove cc from someone's wallet."""
        if not await bank.spend(
            self.bot.db, ctx.guild.id, member.id, amount, bank.KIND_ADMIN, f"Taken by {ctx.author}"
        ):
            held = await bank.balance(self.bot.db, ctx.guild.id, member.id)
            raise FriendlyError(
                f"**{member.display_name}** only has {bank.format_cc(held)}. "
                f"Use `/economy set` to zero them out."
            )
        await ctx.send(embed=embeds.success(f"Took {bank.format_cc(amount)} from **{member.display_name}**."))

    @economy.command(name="set")
    @app_commands.describe(member="Whose balance to set", amount="New balance")
    @commands.has_permissions(manage_guild=True)
    async def economy_set(
        self, ctx: commands.Context, member: discord.Member, amount: commands.Range[int, 0, 10_000_000]
    ) -> None:
        """Set someone's balance outright."""
        await bank.force_set(self.bot.db, ctx.guild.id, member.id, amount, f"Set by {ctx.author}")
        await ctx.send(
            embed=embeds.success(f"**{member.display_name}** now has {bank.format_cc(amount)}.")
        )

    @economy.command(name="rate")
    @app_commands.describe(
        minimum="Lowest cc per message", maximum="Highest cc per message", cooldown="Seconds between payouts"
    )
    @commands.has_permissions(manage_guild=True)
    async def economy_rate(
        self,
        ctx: commands.Context,
        minimum: commands.Range[int, 0, 1000],
        maximum: commands.Range[int, 0, 1000],
        cooldown: commands.Range[int, 0, 86400] = 60,
    ) -> None:
        """Set how much chatting pays."""
        if minimum > maximum:
            raise FriendlyError("The minimum can't be larger than the maximum.")
        await self.bot.db.set_guild_setting(ctx.guild.id, "cc_chat_min", minimum)
        await self.bot.db.set_guild_setting(ctx.guild.id, "cc_chat_max", maximum)
        await self.bot.db.set_guild_setting(ctx.guild.id, "cc_chat_cooldown", cooldown)
        await ctx.send(
            embed=embeds.success(f"Chatting now pays **{minimum}–{maximum} cc** every **{cooldown}s**.")
        )

    @economy.command(name="rewards")
    @app_commands.describe(
        daily="cc for a daily claim",
        streak="Extra cc per day of streak",
        work="Highest cc for a job",
        work_cooldown="Seconds between jobs",
    )
    @commands.has_permissions(manage_guild=True)
    async def economy_rewards(
        self,
        ctx: commands.Context,
        daily: commands.Range[int, 0, 1_000_000],
        streak: commands.Range[int, 0, 100_000] = 25,
        work: commands.Range[int, 0, 1_000_000] = 220,
        work_cooldown: commands.Range[int, 60, 604_800] = 3600,
    ) -> None:
        """Set what /daily and /work pay."""
        await self.bot.db.set_guild_setting(ctx.guild.id, "cc_daily_amount", daily)
        await self.bot.db.set_guild_setting(ctx.guild.id, "cc_daily_streak_bonus", streak)
        await self.bot.db.set_guild_setting(ctx.guild.id, "cc_work_min", work // 4)
        await self.bot.db.set_guild_setting(ctx.guild.id, "cc_work_max", work)
        await self.bot.db.set_guild_setting(ctx.guild.id, "cc_work_cooldown", work_cooldown)
        await ctx.send(
            embed=embeds.success(
                f"Daily pays **{bank.format_cc(daily)}** (+{streak} a day of streak); "
                f"work pays up to **{bank.format_cc(work)}** every "
                f"**{format_long_duration(work_cooldown)}**."
            )
        )

    @economy.command(name="start")
    @app_commands.describe(amount="cc a member starts with")
    @commands.has_permissions(manage_guild=True)
    async def economy_start(
        self, ctx: commands.Context, amount: commands.Range[int, 0, 1_000_000]
    ) -> None:
        """Set the balance new members open their wallet with."""
        await self.bot.db.set_guild_setting(ctx.guild.id, "cc_start_balance", amount)
        await ctx.send(
            embed=embeds.success(f"New wallets now open with {bank.format_cc(amount)}.")
        )

    @economy.command(name="ignore")
    @app_commands.describe(channel="Channel that should stop paying cc for chatting")
    @commands.has_permissions(manage_guild=True)
    async def economy_ignore(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Stop paying cc for messages in a channel."""
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO economy_ignores (guild_id, channel_id) VALUES (?, ?)",
            (ctx.guild.id, channel.id),
        )
        await ctx.send(embed=embeds.success(f"No more cc from {channel.mention}."))

    @economy.command(name="unignore")
    @app_commands.describe(channel="Channel that should pay cc again")
    @commands.has_permissions(manage_guild=True)
    async def economy_unignore(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Pay cc for messages in a channel again."""
        await self.bot.db.execute(
            "DELETE FROM economy_ignores WHERE guild_id = ? AND channel_id = ?",
            (ctx.guild.id, channel.id),
        )
        await ctx.send(embed=embeds.success(f"{channel.mention} pays cc again."))

    @economy.command(name="reset")
    @app_commands.describe(member="Reset just this member. Omit to wipe every wallet.")
    @commands.has_permissions(manage_guild=True)
    async def economy_reset(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        """Wipe wallets, back to the starting balance."""
        config = await self.bot.db.get_guild_config(ctx.guild.id)
        start = int(config.get("cc_start_balance") or 0)

        if member is not None:
            await bank.force_set(
                self.bot.db, ctx.guild.id, member.id, start, f"Reset by {ctx.author}"
            )
            await ctx.send(
                embed=embeds.success(
                    f"**{member.display_name}** is back to {bank.format_cc(start)}."
                )
            )
            return

        view = Confirm(ctx.author.id, confirm_label="Wipe every wallet", confirm_style=discord.ButtonStyle.danger)
        view.message = await ctx.send(
            embed=embeds.warning(
                "This resets **everyone's** cc and wipes the ledger. Open Culshi markets keep "
                "their pots, so settle or cancel them first."
            ),
            view=view,
        )
        await view.wait()
        if not view.confirmed:
            return
        await self.bot.db.execute("DELETE FROM cc_ledger WHERE guild_id = ?", (ctx.guild.id,))
        await self.bot.db.execute("DELETE FROM balances WHERE guild_id = ?", (ctx.guild.id,))
        await ctx.send(embed=embeds.success("Every wallet on this server has been reset."))

    # ── helpers ──────────────────────────────────────────────────────────────

    async def _require_economy(self, ctx: commands.Context) -> None:
        config = await self.bot.db.get_guild_config(ctx.guild.id)
        if not config.get("economy_enabled"):
            raise FriendlyError(
                "Culture Coin is switched off here. Someone with **Manage Server** can run "
                "`/economy toggle on`."
            )


async def setup(bot) -> None:
    await bot.add_cog(Economy(bot))
