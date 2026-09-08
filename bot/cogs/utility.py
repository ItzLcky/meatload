"""Information commands and reminders."""

from __future__ import annotations

import platform
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import __version__
from ..utils import embeds
from ..utils.errors import FriendlyError
from ..utils.formatting import format_long_duration, parse_duration, truncate

MAX_REMINDER = 365 * 86400  # a year is plenty

STATUS_EMOJI = {
    discord.Status.online: "🟢",
    discord.Status.idle: "🟡",
    discord.Status.dnd: "🔴",
    discord.Status.offline: "⚫",
}


class Utility(commands.Cog):
    """Server info, user info, and reminders."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.reminder_loop.start()

    async def cog_unload(self) -> None:
        self.reminder_loop.cancel()

    # ── reminders ────────────────────────────────────────────────────────────

    @tasks.loop(seconds=15)
    async def reminder_loop(self) -> None:
        """Deliver anything that has come due.

        Polling beats an in-memory timer here: reminders live in SQLite, so a
        restart loses nothing and there is no re-scheduling logic to get wrong.
        """
        rows = await self.bot.db.fetchall(
            "SELECT * FROM reminders WHERE remind_at <= ? ORDER BY remind_at LIMIT 25", (time.time(),)
        )
        for row in rows:
            await self.bot.db.execute("DELETE FROM reminders WHERE id = ?", (row["id"],))
            channel = self.bot.get_channel(row["channel_id"])
            if channel is None:
                try:
                    user = await self.bot.fetch_user(row["user_id"])
                    channel = await user.create_dm()
                except discord.HTTPException:
                    continue

            embed = embeds.info(row["content"] or "*no note*", title="⏰ Reminder")
            embed.add_field(
                name="Set", value=f"<t:{int(row['created_at'])}:R>", inline=True
            )
            if row["jump_url"]:
                embed.add_field(name="Context", value=f"[jump]({row['jump_url']})", inline=True)
            try:
                await channel.send(f"<@{row['user_id']}>", embed=embed)
            except discord.HTTPException:
                pass

    @reminder_loop.before_loop
    async def _before_reminders(self) -> None:
        await self.bot.wait_until_ready()

    @commands.hybrid_command(name="remindme", aliases=["remind", "reminder"])
    @app_commands.describe(when="When to remind you: `20m`, `3h`, `2d`", what="What to remind you about")
    async def remindme(self, ctx: commands.Context, when: str, *, what: str = "") -> None:
        """Have the bot ping you later."""
        seconds = parse_duration(when)
        if not seconds:
            raise FriendlyError("I couldn't read that. Try `20m`, `3h`, or `2d`.")
        if seconds > MAX_REMINDER:
            raise FriendlyError("I can only remind you up to a year out.")

        now = time.time()
        await self.bot.db.execute(
            "INSERT INTO reminders (user_id, channel_id, guild_id, jump_url, content, remind_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ctx.author.id,
                ctx.channel.id,
                ctx.guild.id if ctx.guild else None,
                ctx.message.jump_url if ctx.interaction is None else None,
                truncate(what, 1000),
                now + seconds,
                now,
            ),
        )
        await ctx.send(
            embed=embeds.success(
                f"I'll remind you <t:{int(now + seconds)}:R> (in {format_long_duration(seconds)})."
            )
        )

    @commands.hybrid_command(name="reminders")
    async def reminders(self, ctx: commands.Context) -> None:
        """List your pending reminders."""
        rows = await self.bot.db.fetchall(
            "SELECT id, content, remind_at FROM reminders WHERE user_id = ? ORDER BY remind_at LIMIT 20",
            (ctx.author.id,),
        )
        if not rows:
            raise FriendlyError("You have no reminders set.")
        lines = [
            f"`#{row['id']}` <t:{int(row['remind_at'])}:R> — {truncate(row['content'] or '*no note*', 80)}"
            for row in rows
        ]
        await ctx.send(embed=embeds.info("\n".join(lines), title="Your reminders"))

    @commands.hybrid_command(name="forgetme", aliases=["cancelreminder"])
    @app_commands.describe(reminder_id="ID from /reminders")
    async def forgetme(self, ctx: commands.Context, reminder_id: int) -> None:
        """Cancel one of your reminders."""
        cursor = await self.bot.db.execute(
            "DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, ctx.author.id)
        )
        if cursor.rowcount == 0:
            raise FriendlyError(f"You don't have a reminder `#{reminder_id}`.")
        await ctx.send(embed=embeds.success(f"Cancelled reminder `#{reminder_id}`."))

    # ── information ──────────────────────────────────────────────────────────

    @commands.hybrid_command(name="ping")
    async def ping(self, ctx: commands.Context) -> None:
        """Check the bot's latency."""
        start = time.perf_counter()
        message = await ctx.send(embed=embeds.info("Pinging…"))
        elapsed = (time.perf_counter() - start) * 1000
        await message.edit(
            embed=embeds.info(
                f"**Gateway:** {self.bot.latency * 1000:.0f} ms\n**Round trip:** {elapsed:.0f} ms",
                title="🏓 Pong",
            )
        )

    @commands.hybrid_command(name="botinfo", aliases=["about", "uptime", "stats"])
    async def botinfo(self, ctx: commands.Context) -> None:
        """Show bot statistics."""
        members = sum(guild.member_count or 0 for guild in self.bot.guilds)
        music = self.bot.get_cog("Music")
        players = len(music.players) if music else 0

        embed = embeds.info("", title=self.bot.user.name)
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.add_field(name="Uptime", value=format_long_duration(time.time() - self.bot.started_at), inline=True)
        embed.add_field(name="Latency", value=f"{self.bot.latency * 1000:.0f} ms", inline=True)
        embed.add_field(name="Servers", value=f"{len(self.bot.guilds)}", inline=True)
        embed.add_field(name="Members", value=f"{members:,}", inline=True)
        embed.add_field(name="Voice players", value=str(players), inline=True)
        embed.add_field(name="Commands", value=str(len(set(self.bot.walk_commands()))), inline=True)
        embed.set_footer(
            text=f"v{__version__} • discord.py {discord.__version__} • Python {platform.python_version()}"
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="avatar", aliases=["pfp"])
    @app_commands.describe(member="Whose avatar to show. Defaults to you.")
    async def avatar(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        """Show someone's avatar."""
        member = member or ctx.author
        embed = embeds.info("", title=f"{member.display_name}'s avatar")
        embed.set_image(url=member.display_avatar.url)
        embed.description = f"[Open original]({member.display_avatar.url})"
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="userinfo", aliases=["whois", "ui"])
    @app_commands.describe(member="Who to look up. Defaults to you.")
    @commands.guild_only()
    async def userinfo(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        """Show details about a member."""
        member = member or ctx.author

        roles = [role.mention for role in reversed(member.roles) if not role.is_default()]
        embed = embeds.info("", title=str(member))
        embed.color = member.color if member.color.value else embeds.BLURPLE
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
        embed.add_field(
            name="Status",
            value=f"{STATUS_EMOJI.get(member.status, '⚫')} {member.status}",
            inline=True,
        )
        embed.add_field(name="Bot", value="yes" if member.bot else "no", inline=True)
        embed.add_field(
            name="Account created", value=f"<t:{int(member.created_at.timestamp())}:D>", inline=True
        )
        if member.joined_at:
            embed.add_field(
                name="Joined server", value=f"<t:{int(member.joined_at.timestamp())}:D>", inline=True
            )
        if member.premium_since:
            embed.add_field(
                name="Boosting since", value=f"<t:{int(member.premium_since.timestamp())}:D>", inline=True
            )
        if member.timed_out_until:
            embed.add_field(
                name="Timed out until",
                value=f"<t:{int(member.timed_out_until.timestamp())}:f>",
                inline=True,
            )
        embed.add_field(
            name=f"Roles ({len(roles)})",
            value=truncate(" ".join(roles), 1000) if roles else "*none*",
            inline=False,
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="serverinfo", aliases=["guildinfo", "si"])
    @commands.guild_only()
    async def serverinfo(self, ctx: commands.Context) -> None:
        """Show details about this server."""
        guild = ctx.guild
        bots = sum(1 for member in guild.members if member.bot)

        embed = embeds.info(guild.description or "", title=guild.name)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="ID", value=f"`{guild.id}`", inline=True)
        embed.add_field(name="Owner", value=guild.owner.mention if guild.owner else "unknown", inline=True)
        embed.add_field(name="Created", value=f"<t:{int(guild.created_at.timestamp())}:D>", inline=True)
        embed.add_field(
            name="Members",
            value=f"{guild.member_count:,} ({bots} bot{'s' if bots != 1 else ''})",
            inline=True,
        )
        embed.add_field(
            name="Channels",
            value=f"{len(guild.text_channels)} text • {len(guild.voice_channels)} voice",
            inline=True,
        )
        embed.add_field(name="Roles", value=str(len(guild.roles) - 1), inline=True)
        embed.add_field(
            name="Boosts",
            value=f"{guild.premium_subscription_count} (tier {guild.premium_tier})",
            inline=True,
        )
        embed.add_field(name="Emoji", value=f"{len(guild.emojis)}/{guild.emoji_limit}", inline=True)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="roleinfo")
    @app_commands.describe(role="Role to look up")
    @commands.guild_only()
    async def roleinfo(self, ctx: commands.Context, *, role: discord.Role) -> None:
        """Show details about a role."""
        embed = embeds.info("", title=role.name)
        embed.color = role.color if role.color.value else embeds.BLURPLE
        embed.add_field(name="ID", value=f"`{role.id}`", inline=True)
        embed.add_field(name="Members", value=str(len(role.members)), inline=True)
        embed.add_field(name="Colour", value=str(role.color), inline=True)
        embed.add_field(name="Position", value=str(role.position), inline=True)
        embed.add_field(name="Mentionable", value="yes" if role.mentionable else "no", inline=True)
        embed.add_field(name="Hoisted", value="yes" if role.hoist else "no", inline=True)
        embed.add_field(name="Created", value=f"<t:{int(role.created_at.timestamp())}:D>", inline=False)
        await ctx.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(Utility(bot))
