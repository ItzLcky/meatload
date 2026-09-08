"""Message-based XP and levels."""

from __future__ import annotations

import random
import time
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from ..utils import embeds
from ..utils.errors import FriendlyError
from ..utils.formatting import format_long_duration, progress_bar, truncate
from ..utils.paginator import send_pages

DEFAULT_LEVELUP = "GG {user}, you reached **level {level}**!"

# Announcement channel encodes three states in one column:
#   NULL -> reply in whatever channel they levelled up in
#   0    -> announcements off
#   id   -> always announce in that channel
ANNOUNCE_DISABLED = 0


def xp_for_level(level: int) -> int:
    """Total XP needed to reach `level`, using the familiar 5n² + 50n + 100 curve."""
    if level <= 0:
        return 0
    n = level
    return (5 * (n - 1) * n * (2 * n - 1)) // 6 + (50 * (n - 1) * n) // 2 + 100 * n


def level_from_xp(xp: int) -> int:
    level = 0
    while xp >= xp_for_level(level + 1):
        level += 1
        if level > 1000:  # guard against a runaway loop if the curve ever changes
            break
    return level


class Leveling(commands.Cog):
    """Earn XP for chatting, level up, and unlock role rewards."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # ── XP awarding ──────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not message.content:
            return

        config = await self.bot.db.get_guild_config(message.guild.id)
        if not config.get("leveling_enabled"):
            return

        ignored = await self.bot.db.fetchone(
            "SELECT 1 FROM level_ignores WHERE guild_id = ? AND channel_id = ?",
            (message.guild.id, message.channel.id),
        )
        if ignored is not None:
            return

        now = time.time()
        row = await self.bot.db.fetchone(
            "SELECT xp, last_xp_at FROM levels WHERE guild_id = ? AND user_id = ?",
            (message.guild.id, message.author.id),
        )
        cooldown = config.get("xp_cooldown") or 60
        if row is not None and now - row["last_xp_at"] < cooldown:
            # Still count the message even when the XP is on cooldown.
            await self.bot.db.execute(
                "UPDATE levels SET messages = messages + 1 WHERE guild_id = ? AND user_id = ?",
                (message.guild.id, message.author.id),
            )
            return

        gain = random.randint(config.get("xp_min") or 15, max(config.get("xp_min") or 15, config.get("xp_max") or 25))
        old_xp = row["xp"] if row else 0
        new_xp = old_xp + gain

        await self.bot.db.execute(
            "INSERT INTO levels (guild_id, user_id, xp, messages, last_xp_at) VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "xp = xp + ?, messages = messages + 1, last_xp_at = ?",
            (message.guild.id, message.author.id, gain, now, gain, now),
        )

        old_level, new_level = level_from_xp(old_xp), level_from_xp(new_xp)
        if new_level > old_level:
            await self._on_level_up(message, config, new_level)

    async def _on_level_up(self, message: discord.Message, config: dict, level: int) -> None:
        member = message.author
        assert isinstance(member, discord.Member)

        awarded = await self._apply_rewards(member, level)

        channel_id = config.get("levelup_channel_id")
        if channel_id == ANNOUNCE_DISABLED:
            return
        channel = message.guild.get_channel(channel_id) if channel_id else message.channel
        if not isinstance(channel, discord.abc.Messageable):
            return

        template = config.get("levelup_message") or DEFAULT_LEVELUP
        text = (
            template.replace("{user}", member.mention)
            .replace("{user.name}", member.display_name)
            .replace("{level}", str(level))
            .replace("{server}", message.guild.name)
        )
        if awarded:
            text += f"\nUnlocked: {', '.join(f'**{role.name}**' for role in awarded)}"

        try:
            await channel.send(
                text, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True)
            )
        except discord.HTTPException:
            pass

    async def _apply_rewards(self, member: discord.Member, level: int) -> list[discord.Role]:
        """Grant every reward role at or below the new level that they don't already have."""
        rows = await self.bot.db.fetchall(
            "SELECT role_id FROM level_rewards WHERE guild_id = ? AND level <= ?", (member.guild.id, level)
        )
        if not rows:
            return []

        me = member.guild.me
        wanted = []
        for row in rows:
            role = member.guild.get_role(row["role_id"])
            if role is not None and role not in member.roles and role < me.top_role:
                wanted.append(role)
        if not wanted:
            return []
        try:
            await member.add_roles(*wanted, reason=f"Level {level} reward")
        except discord.HTTPException:
            return []
        return wanted

    # ── user-facing ──────────────────────────────────────────────────────────

    @commands.hybrid_command(name="rank", aliases=["level", "xp"])
    @app_commands.describe(member="Whose rank to show. Defaults to you.")
    @commands.guild_only()
    async def rank(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        """Show someone's level and progress."""
        member = member or ctx.author
        row = await self.bot.db.fetchone(
            "SELECT xp, messages FROM levels WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, member.id)
        )
        if row is None or row["xp"] == 0:
            raise FriendlyError(f"**{member.display_name}** hasn't earned any XP yet.")

        xp = row["xp"]
        level = level_from_xp(xp)
        floor, ceiling = xp_for_level(level), xp_for_level(level + 1)
        into, needed = xp - floor, ceiling - floor

        position = await self.bot.db.fetchval(
            "SELECT COUNT(*) + 1 FROM levels WHERE guild_id = ? AND xp > ?", (ctx.guild.id, xp), default=1
        )

        embed = embeds.info(
            f"`{into:,} / {needed:,} XP` {progress_bar(into, needed)}", title=f"Rank — {member.display_name}"
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Level", value=f"**{level}**", inline=True)
        embed.add_field(name="Rank", value=f"**#{position}**", inline=True)
        embed.add_field(name="Total XP", value=f"{xp:,}", inline=True)
        embed.add_field(name="Messages", value=f"{row['messages']:,}", inline=True)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="leaderboard", aliases=["lb", "top", "levels"])
    @commands.guild_only()
    async def leaderboard(self, ctx: commands.Context) -> None:
        """Show the server's XP leaderboard."""
        rows = await self.bot.db.fetchall(
            "SELECT user_id, xp, messages FROM levels WHERE guild_id = ? AND xp > 0 ORDER BY xp DESC LIMIT 100",
            (ctx.guild.id,),
        )
        if not rows:
            raise FriendlyError("Nobody has earned XP yet.")

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        pages: list[discord.Embed] = []
        per_page = 10
        for start in range(0, len(rows), per_page):
            chunk = rows[start : start + per_page]
            lines = []
            for offset, row in enumerate(chunk):
                place = start + offset + 1
                prefix = medals.get(place, f"`#{place}`")
                lines.append(
                    f"{prefix} <@{row['user_id']}> — level **{level_from_xp(row['xp'])}** "
                    f"({row['xp']:,} XP)"
                )
            embed = embeds.info("\n".join(lines), title=f"{ctx.guild.name} leaderboard")
            pages.append(embed)
        await send_pages(ctx, pages)

    # ── configuration ────────────────────────────────────────────────────────

    @commands.hybrid_group(name="leveling", aliases=["levelling"], fallback="show", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def leveling(self, ctx: commands.Context) -> None:
        """Configure the XP system."""
        config = await self.bot.db.get_guild_config(ctx.guild.id)
        channel_id = config.get("levelup_channel_id")
        if channel_id == ANNOUNCE_DISABLED:
            announce = "*off*"
        elif channel_id:
            announce = f"<#{channel_id}>"
        else:
            announce = "wherever they levelled up"

        ignores = await self.bot.db.fetchall(
            "SELECT channel_id FROM level_ignores WHERE guild_id = ?", (ctx.guild.id,)
        )
        rewards = await self.bot.db.fetchall(
            "SELECT level, role_id FROM level_rewards WHERE guild_id = ? ORDER BY level", (ctx.guild.id,)
        )

        embed = embeds.info(
            f"**Enabled:** {'yes' if config.get('leveling_enabled') else 'no'}\n"
            f"**Announcements:** {announce}\n"
            f"**XP per message:** {config.get('xp_min')}–{config.get('xp_max')} "
            f"every {format_long_duration(config.get('xp_cooldown') or 60)}\n"
            f"**Message:** {truncate(config.get('levelup_message') or DEFAULT_LEVELUP, 200)}",
            title="Leveling settings",
        )
        if rewards:
            embed.add_field(
                name="Role rewards",
                value="\n".join(f"Level **{row['level']}** → <@&{row['role_id']}>" for row in rewards),
                inline=False,
            )
        if ignores:
            embed.add_field(
                name="Ignored channels",
                value=" ".join(f"<#{row['channel_id']}>" for row in ignores),
                inline=False,
            )
        await ctx.send(embed=embed)

    @leveling.command(name="toggle")
    @app_commands.describe(state="on or off")
    async def leveling_toggle(self, ctx: commands.Context, state: Literal["on", "off"]) -> None:
        """Turn the XP system on or off."""
        await self.bot.db.set_guild_setting(ctx.guild.id, "leveling_enabled", 1 if state == "on" else 0)
        await ctx.send(embed=embeds.success(f"Leveling is now **{state}**."))

    @leveling.command(name="announce")
    @app_commands.describe(
        where="A channel to always announce in, `here` to reply in place, or `off` to stay quiet",
        channel="The channel, when `where` is a channel",
    )
    async def leveling_announce(
        self,
        ctx: commands.Context,
        where: Literal["channel", "here", "off"],
        channel: discord.TextChannel | None = None,
    ) -> None:
        """Choose where level-ups are announced."""
        if where == "off":
            value, description = ANNOUNCE_DISABLED, "Level-ups won't be announced."
        elif where == "here":
            value, description = None, "Level-ups are announced wherever they happen."
        else:
            if channel is None:
                raise FriendlyError("Pass the channel too, e.g. `/leveling announce channel #general`.")
            value, description = channel.id, f"Level-ups will be announced in {channel.mention}."
        await self.bot.db.set_guild_setting(ctx.guild.id, "levelup_channel_id", value)
        await ctx.send(embed=embeds.success(description))

    @leveling.command(name="message")
    @app_commands.describe(message="Template. Use {user}, {user.name}, {level}, {server}.")
    async def leveling_message(self, ctx: commands.Context, *, message: str) -> None:
        """Set the level-up message."""
        if len(message) > 500:
            raise FriendlyError("Keep the level-up message under 500 characters.")
        await self.bot.db.set_guild_setting(ctx.guild.id, "levelup_message", message)
        await ctx.send(embed=embeds.success("Level-up message set."))

    @leveling.command(name="rate")
    @app_commands.describe(
        minimum="Lowest XP per message", maximum="Highest XP per message", cooldown="Seconds between awards"
    )
    async def leveling_rate(
        self,
        ctx: commands.Context,
        minimum: commands.Range[int, 0, 500],
        maximum: commands.Range[int, 0, 500],
        cooldown: commands.Range[int, 0, 3600] = 60,
    ) -> None:
        """Set how fast XP accrues."""
        if minimum > maximum:
            raise FriendlyError("The minimum can't be larger than the maximum.")
        await self.bot.db.set_guild_setting(ctx.guild.id, "xp_min", minimum)
        await self.bot.db.set_guild_setting(ctx.guild.id, "xp_max", maximum)
        await self.bot.db.set_guild_setting(ctx.guild.id, "xp_cooldown", cooldown)
        await ctx.send(
            embed=embeds.success(f"Members now earn **{minimum}–{maximum} XP** every **{cooldown}s**.")
        )

    @leveling.command(name="ignore")
    @app_commands.describe(channel="Channel that should stop granting XP")
    async def leveling_ignore(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Stop awarding XP in a channel."""
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO level_ignores (guild_id, channel_id) VALUES (?, ?)",
            (ctx.guild.id, channel.id),
        )
        await ctx.send(embed=embeds.success(f"No more XP from {channel.mention}."))

    @leveling.command(name="unignore")
    @app_commands.describe(channel="Channel that should grant XP again")
    async def leveling_unignore(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Award XP in a channel again."""
        await self.bot.db.execute(
            "DELETE FROM level_ignores WHERE guild_id = ? AND channel_id = ?", (ctx.guild.id, channel.id)
        )
        await ctx.send(embed=embeds.success(f"{channel.mention} grants XP again."))

    @leveling.command(name="reward")
    @app_commands.describe(level="Level that unlocks the role", role="Role to grant")
    @commands.bot_has_permissions(manage_roles=True)
    async def leveling_reward(
        self, ctx: commands.Context, level: commands.Range[int, 1, 1000], role: discord.Role
    ) -> None:
        """Grant a role when someone reaches a level."""
        if role >= ctx.guild.me.top_role:
            raise FriendlyError(f"**{role.name}** is above my highest role, so I can't grant it.")
        if role.managed or role.is_default():
            raise FriendlyError("That role can't be granted by a bot.")
        await self.bot.db.execute(
            "INSERT INTO level_rewards (guild_id, level, role_id) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, level) DO UPDATE SET role_id = ?",
            (ctx.guild.id, level, role.id, role.id),
        )
        await ctx.send(embed=embeds.success(f"Level **{level}** now grants **{role.name}**."))

    @leveling.command(name="unreward")
    @app_commands.describe(level="Level whose reward should be removed")
    async def leveling_unreward(self, ctx: commands.Context, level: int) -> None:
        """Remove a level's role reward."""
        cursor = await self.bot.db.execute(
            "DELETE FROM level_rewards WHERE guild_id = ? AND level = ?", (ctx.guild.id, level)
        )
        if cursor.rowcount == 0:
            raise FriendlyError(f"No reward is set for level {level}.")
        await ctx.send(embed=embeds.success(f"Removed the level {level} reward."))

    @leveling.command(name="setxp")
    @app_commands.describe(member="Whose XP to set", xp="New total XP")
    async def leveling_setxp(
        self, ctx: commands.Context, member: discord.Member, xp: commands.Range[int, 0, 100_000_000]
    ) -> None:
        """Set a member's total XP."""
        await self.bot.db.execute(
            "INSERT INTO levels (guild_id, user_id, xp, last_xp_at) VALUES (?, ?, ?, 0) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = ?",
            (ctx.guild.id, member.id, xp, xp),
        )
        await ctx.send(
            embed=embeds.success(
                f"**{member.display_name}** is now at **{xp:,} XP** (level {level_from_xp(xp)})."
            )
        )

    @leveling.command(name="reset")
    @app_commands.describe(member="Reset just this member. Omit to reset the whole server.")
    async def leveling_reset(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        """Wipe XP for one member or the entire server."""
        if member is not None:
            await self.bot.db.execute(
                "DELETE FROM levels WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, member.id)
            )
            await ctx.send(embed=embeds.success(f"Reset XP for **{member.display_name}**."))
            return

        view = ConfirmReset(ctx.author.id)
        view.message = await ctx.send(
            embed=embeds.warning("This wipes **everyone's** XP on this server. Are you sure?"),
            view=view,
        )
        await view.wait()
        if not view.confirmed:
            return
        await self.bot.db.execute("DELETE FROM levels WHERE guild_id = ?", (ctx.guild.id,))
        await ctx.send(embed=embeds.success("Wiped all XP on this server."))


class ConfirmReset(discord.ui.View):
    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=30.0)
        self.author_id = author_id
        self.confirmed = False
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(embed=embeds.neutral("Timed out — nothing was reset."), view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Wipe all XP", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.confirmed = True
        await interaction.response.edit_message(embed=embeds.neutral("Resetting…"), view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=embeds.neutral("Cancelled."), view=None)
        self.stop()


async def setup(bot) -> None:
    await bot.add_cog(Leveling(bot))
