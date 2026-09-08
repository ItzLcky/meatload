"""Moderation commands with a persistent, numbered case history."""

from __future__ import annotations

import datetime as dt
import time

import discord
from discord import app_commands
from discord.ext import commands

from ..utils import embeds
from ..utils.checks import can_act_on
from ..utils.errors import FriendlyError
from ..utils.formatting import format_long_duration, parse_duration, truncate
from ..utils.paginator import send_pages

MAX_TIMEOUT = dt.timedelta(days=28)  # Discord's hard limit

ACTION_STYLE = {
    "ban": ("🔨", embeds.RED),
    "unban": ("🕊️", embeds.GREEN),
    "kick": ("👢", embeds.YELLOW),
    "timeout": ("🔇", embeds.YELLOW),
    "untimeout": ("🔊", embeds.GREEN),
    "warn": ("⚠️", embeds.YELLOW),
    "softban": ("🧹", embeds.YELLOW),
    "purge": ("🗑️", embeds.GREY),
}


class Moderation(commands.Cog):
    """Kick, ban, timeout, warn, and purge — all recorded as numbered cases."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # ── case bookkeeping ─────────────────────────────────────────────────────

    async def log_case(
        self,
        guild: discord.Guild,
        action: str,
        target: discord.abc.User,
        moderator: discord.abc.User,
        reason: str | None,
        duration: int | None = None,
    ) -> int:
        """Record a case and mirror it to the mod-log channel. Returns its number."""
        next_number = (
            await self.bot.db.fetchval(
                "SELECT COALESCE(MAX(case_number), 0) + 1 FROM mod_cases WHERE guild_id = ?",
                (guild.id,),
                default=1,
            )
            or 1
        )
        await self.bot.db.execute(
            "INSERT INTO mod_cases "
            "(guild_id, case_number, action, target_id, moderator_id, reason, duration_seconds, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guild.id, next_number, action, target.id, moderator.id, reason, duration, time.time()),
        )

        config = await self.bot.db.get_guild_config(guild.id)
        channel_id = config.get("modlog_channel_id")
        if channel_id:
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.abc.Messageable):
                emoji, color = ACTION_STYLE.get(action, ("📋", embeds.GREY))
                embed = discord.Embed(
                    color=color,
                    title=f"{emoji} {action.title()} — case #{next_number}",
                    timestamp=dt.datetime.now(dt.timezone.utc),
                )
                embed.add_field(name="User", value=f"{target.mention}\n`{target}` (`{target.id}`)", inline=True)
                embed.add_field(name="Moderator", value=moderator.mention, inline=True)
                if duration:
                    embed.add_field(name="Duration", value=format_long_duration(duration), inline=True)
                embed.add_field(name="Reason", value=truncate(reason or "No reason given", 1000), inline=False)
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException:
                    pass

        return next_number

    @staticmethod
    async def notify_target(
        target: discord.abc.User, guild: discord.Guild, action: str, reason: str | None
    ) -> bool:
        """Best-effort DM. Plenty of people have DMs closed; that isn't an error."""
        try:
            await target.send(
                embed=embeds.warning(
                    f"You were **{action}** in **{guild.name}**.\n"
                    f"**Reason:** {truncate(reason or 'No reason given', 500)}"
                )
            )
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    def _check_hierarchy(self, ctx: commands.Context, target: discord.abc.User) -> None:
        if isinstance(target, discord.Member):
            problem = can_act_on(ctx.author, target)
            if problem:
                raise FriendlyError(problem)

    # ── commands ─────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="kick")
    @app_commands.describe(member="Who to kick", reason="Shown in the audit log and their DM")
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    async def kick(
        self, ctx: commands.Context, member: discord.Member, *, reason: str | None = None
    ) -> None:
        """Kick a member."""
        self._check_hierarchy(ctx, member)
        await ctx.defer()
        dm_sent = await self.notify_target(member, ctx.guild, "kicked", reason)
        await member.kick(reason=f"{ctx.author} ({ctx.author.id}): {reason or 'no reason'}")
        case = await self.log_case(ctx.guild, "kick", member, ctx.author, reason)
        await ctx.send(
            embed=embeds.success(
                f"Kicked **{member}** — case #{case}." + ("" if dm_sent else "\n*Couldn't DM them.*")
            )
        )

    @commands.hybrid_command(name="ban")
    @app_commands.describe(
        user="Who to ban (a member, or a user ID to pre-emptively ban)",
        delete_days="Days of their messages to delete, 0-7",
        reason="Shown in the audit log and their DM",
    )
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def ban(
        self,
        ctx: commands.Context,
        user: discord.User,
        delete_days: commands.Range[int, 0, 7] = 0,
        *,
        reason: str | None = None,
    ) -> None:
        """Ban a user, even one who isn't in the server."""
        # Invoked as a slash command, `user` is a User even for someone in the
        # server, so resolve the Member before checking role hierarchy.
        member = ctx.guild.get_member(user.id)
        if member is not None:
            self._check_hierarchy(ctx, member)
        await ctx.defer()

        dm_sent = False
        if member is not None:
            dm_sent = await self.notify_target(member, ctx.guild, "banned", reason)
        try:
            await ctx.guild.ban(
                user,
                reason=f"{ctx.author} ({ctx.author.id}): {reason or 'no reason'}",
                delete_message_seconds=delete_days * 86400,
            )
        except discord.NotFound as exc:
            raise FriendlyError("That user doesn't exist.") from exc

        case = await self.log_case(ctx.guild, "ban", user, ctx.author, reason)
        note = "" if dm_sent or member is None else "\n*Couldn't DM them.*"
        await ctx.send(embed=embeds.success(f"Banned **{user}** — case #{case}.{note}"))

    @commands.hybrid_command(name="softban")
    @app_commands.describe(member="Who to softban", reason="Why")
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def softban(
        self, ctx: commands.Context, member: discord.Member, *, reason: str | None = None
    ) -> None:
        """Ban then immediately unban, which kicks the member and wipes their recent messages."""
        self._check_hierarchy(ctx, member)
        await ctx.defer()
        await self.notify_target(member, ctx.guild, "softbanned (kicked, messages cleared)", reason)
        audit = f"Softban by {ctx.author} ({ctx.author.id}): {reason or 'no reason'}"
        await ctx.guild.ban(member, reason=audit, delete_message_seconds=86400)
        await ctx.guild.unban(member, reason=audit)
        case = await self.log_case(ctx.guild, "softban", member, ctx.author, reason)
        await ctx.send(embed=embeds.success(f"Softbanned **{member}** — case #{case}."))

    @commands.hybrid_command(name="unban")
    @app_commands.describe(user="User ID or name#tag to unban", reason="Why")
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def unban(
        self, ctx: commands.Context, user: discord.User, *, reason: str | None = None
    ) -> None:
        """Lift a ban."""
        await ctx.defer()
        try:
            await ctx.guild.unban(user, reason=f"{ctx.author} ({ctx.author.id}): {reason or 'no reason'}")
        except discord.NotFound as exc:
            raise FriendlyError(f"**{user}** isn't banned.") from exc
        case = await self.log_case(ctx.guild, "unban", user, ctx.author, reason)
        await ctx.send(embed=embeds.success(f"Unbanned **{user}** — case #{case}."))

    @commands.hybrid_command(name="timeout", aliases=["mute"])
    @app_commands.describe(
        member="Who to time out",
        duration="How long: `10m`, `2h`, `1d`. Max 28 days.",
        reason="Why",
    )
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def timeout(
        self, ctx: commands.Context, member: discord.Member, duration: str, *, reason: str | None = None
    ) -> None:
        """Temporarily stop a member from talking."""
        self._check_hierarchy(ctx, member)
        seconds = parse_duration(duration)
        if not seconds:
            raise FriendlyError("I couldn't read that duration. Try `10m`, `2h`, or `1d`.")
        delta = dt.timedelta(seconds=seconds)
        if delta > MAX_TIMEOUT:
            raise FriendlyError("Discord caps timeouts at 28 days.")

        await ctx.defer()
        await member.timeout(delta, reason=f"{ctx.author} ({ctx.author.id}): {reason or 'no reason'}")
        await self.notify_target(
            member, ctx.guild, f"timed out for {format_long_duration(seconds)}", reason
        )
        case = await self.log_case(ctx.guild, "timeout", member, ctx.author, reason, duration=seconds)
        until = discord.utils.utcnow() + delta
        await ctx.send(
            embed=embeds.success(
                f"Timed out **{member}** until <t:{int(until.timestamp())}:f> — case #{case}."
            )
        )

    @commands.hybrid_command(name="untimeout", aliases=["unmute"])
    @app_commands.describe(member="Who to release", reason="Why")
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def untimeout(
        self, ctx: commands.Context, member: discord.Member, *, reason: str | None = None
    ) -> None:
        """End a timeout early."""
        if member.timed_out_until is None:
            raise FriendlyError(f"**{member}** isn't timed out.")
        await ctx.defer()
        await member.timeout(None, reason=f"{ctx.author} ({ctx.author.id}): {reason or 'no reason'}")
        case = await self.log_case(ctx.guild, "untimeout", member, ctx.author, reason)
        await ctx.send(embed=embeds.success(f"Removed the timeout on **{member}** — case #{case}."))

    @commands.hybrid_command(name="warn")
    @app_commands.describe(member="Who to warn", reason="What they did")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str) -> None:
        """Record a warning against a member."""
        self._check_hierarchy(ctx, member)
        await ctx.defer()
        dm_sent = await self.notify_target(member, ctx.guild, "warned", reason)
        case = await self.log_case(ctx.guild, "warn", member, ctx.author, reason)
        total = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM mod_cases WHERE guild_id = ? AND target_id = ? AND action = 'warn'",
            (ctx.guild.id, member.id),
            default=0,
        )
        await ctx.send(
            embed=embeds.success(
                f"Warned **{member}** — case #{case}. They now have **{total}** warning(s)."
                + ("" if dm_sent else "\n*Couldn't DM them.*")
            )
        )

    @commands.hybrid_command(name="warnings", aliases=["infractions", "history"])
    @app_commands.describe(member="Whose history to show. Defaults to you.")
    @commands.guild_only()
    async def warnings(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        """Show a member's moderation history."""
        member = member or ctx.author
        # Anyone can look up their own record; seeing others' needs the permission.
        if member.id != ctx.author.id and not ctx.author.guild_permissions.manage_messages:
            raise FriendlyError("You need **Manage Messages** to view someone else's history.")

        rows = await self.bot.db.fetchall(
            "SELECT * FROM mod_cases WHERE guild_id = ? AND target_id = ? ORDER BY case_number DESC",
            (ctx.guild.id, member.id),
        )
        if not rows:
            await ctx.send(embed=embeds.info(f"**{member}** has a clean record. ✨"))
            return

        pages: list[discord.Embed] = []
        per_page = 6
        for start in range(0, len(rows), per_page):
            chunk = rows[start : start + per_page]
            lines = []
            for row in chunk:
                emoji = ACTION_STYLE.get(row["action"], ("📋", None))[0]
                line = (
                    f"{emoji} **#{row['case_number']} · {row['action']}** — <t:{int(row['created_at'])}:R>\n"
                    f"by <@{row['moderator_id']}>"
                )
                if row["duration_seconds"]:
                    line += f" for {format_long_duration(row['duration_seconds'])}"
                line += f"\n> {truncate(row['reason'] or 'No reason given', 200)}"
                lines.append(line)
            embed = embeds.info("\n\n".join(lines), title=f"History for {member}")
            embed.set_footer(text=f"{len(rows)} case(s) total")
            pages.append(embed)
        await send_pages(ctx, pages)

    @commands.hybrid_command(name="delcase", aliases=["delwarn"])
    @app_commands.describe(case_number="Case number to delete, from /warnings")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def delcase(self, ctx: commands.Context, case_number: int) -> None:
        """Delete a case from the history."""
        cursor = await self.bot.db.execute(
            "DELETE FROM mod_cases WHERE guild_id = ? AND case_number = ?", (ctx.guild.id, case_number)
        )
        if cursor.rowcount == 0:
            raise FriendlyError(f"There's no case #{case_number}.")
        await ctx.send(embed=embeds.success(f"Deleted case #{case_number}."))

    @commands.hybrid_command(name="reason")
    @app_commands.describe(case_number="Case to edit", reason="The corrected reason")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def reason(self, ctx: commands.Context, case_number: int, *, reason: str) -> None:
        """Set or correct the reason on an existing case."""
        cursor = await self.bot.db.execute(
            "UPDATE mod_cases SET reason = ? WHERE guild_id = ? AND case_number = ?",
            (reason, ctx.guild.id, case_number),
        )
        if cursor.rowcount == 0:
            raise FriendlyError(f"There's no case #{case_number}.")
        await ctx.send(embed=embeds.success(f"Updated the reason on case #{case_number}."))

    @commands.hybrid_command(name="purge", aliases=["clean", "prune"])
    @app_commands.describe(
        amount="How many messages to check, 1-100",
        member="Only delete messages from this member",
        contains="Only delete messages containing this text",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True, read_message_history=True)
    async def purge(
        self,
        ctx: commands.Context,
        amount: commands.Range[int, 1, 100],
        member: discord.Member | None = None,
        *,
        contains: str | None = None,
    ) -> None:
        """Bulk-delete recent messages."""
        await ctx.defer(ephemeral=True)

        needle = contains.lower() if contains else None

        def predicate(message: discord.Message) -> bool:
            if member is not None and message.author.id != member.id:
                return False
            if needle is not None and needle not in message.content.lower():
                return False
            return True

        # Discord refuses to bulk-delete anything older than 14 days.
        cutoff = discord.utils.utcnow() - dt.timedelta(days=13, hours=23)
        try:
            deleted = await ctx.channel.purge(limit=amount, check=predicate, after=cutoff, reason=str(ctx.author))
        except discord.HTTPException as exc:
            raise FriendlyError(f"Couldn't delete those messages: {exc}") from exc

        await self.log_case(
            ctx.guild, "purge", ctx.author, ctx.author, f"{len(deleted)} messages in #{ctx.channel}"
        )
        await ctx.send(
            embed=embeds.success(f"Deleted **{len(deleted)}** message(s)."), ephemeral=True, delete_after=10
        )

    @commands.hybrid_command(name="slowmode")
    @app_commands.describe(duration="Delay between messages, e.g. `10s`, `1m`, or `off`")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def slowmode(self, ctx: commands.Context, duration: str = "off") -> None:
        """Set this channel's slowmode."""
        if duration.lower() in {"off", "0", "none", "disable"}:
            seconds = 0
        else:
            parsed = parse_duration(duration)
            if parsed is None:
                raise FriendlyError("I couldn't read that duration. Try `10s`, `1m`, or `off`.")
            seconds = parsed
        if seconds > 21600:
            raise FriendlyError("Discord caps slowmode at 6 hours.")

        await ctx.channel.edit(slowmode_delay=seconds, reason=str(ctx.author))
        if seconds:
            await ctx.send(embed=embeds.success(f"Slowmode set to **{format_long_duration(seconds)}**."))
        else:
            await ctx.send(embed=embeds.success("Slowmode disabled."))

    @commands.hybrid_command(name="lock")
    @app_commands.describe(reason="Shown in the channel")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def lock(self, ctx: commands.Context, *, reason: str | None = None) -> None:
        """Stop @everyone from sending messages here."""
        overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
        if overwrite.send_messages is False:
            raise FriendlyError("This channel is already locked.")
        overwrite.send_messages = False
        await ctx.channel.set_permissions(
            ctx.guild.default_role, overwrite=overwrite, reason=f"Locked by {ctx.author}"
        )
        await ctx.send(
            embed=embeds.warning(f"🔒 Channel locked.{f' — {reason}' if reason else ''}")
        )

    @commands.hybrid_command(name="unlock")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def unlock(self, ctx: commands.Context) -> None:
        """Let @everyone send messages here again."""
        overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
        # Back to None (inherit) rather than True, so category permissions still apply.
        overwrite.send_messages = None
        await ctx.channel.set_permissions(
            ctx.guild.default_role, overwrite=overwrite, reason=f"Unlocked by {ctx.author}"
        )
        await ctx.send(embed=embeds.success("🔓 Channel unlocked."))


async def setup(bot) -> None:
    await bot.add_cog(Moderation(bot))
