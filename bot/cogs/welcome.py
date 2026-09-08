"""Join/leave announcements and automatic roles."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..utils import embeds
from ..utils.errors import FriendlyError

DEFAULT_WELCOME = "Welcome to **{server}**, {user}! You're member #{count}."
DEFAULT_LEAVE = "**{user.name}** left the server."

PLACEHOLDERS = (
    "`{user}` mention • `{user.name}` display name • `{user.tag}` username • `{user.id}`\n"
    "`{server}` server name • `{count}` member count"
)


def render(template: str, member: discord.Member) -> str:
    replacements = {
        "{user}": member.mention,
        "{user.name}": member.display_name,
        "{user.tag}": str(member),
        "{user.id}": str(member.id),
        "{server}": member.guild.name,
        "{count}": str(member.guild.member_count or 0),
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


class Welcome(commands.Cog):
    """Greet people when they arrive and note when they leave."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # ── listeners ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        config = await self.bot.db.get_guild_config(member.guild.id)

        role_id = config.get("autorole_id")
        if role_id:
            role = member.guild.get_role(role_id)
            # Skip silently if the role vanished or sits above the bot: this
            # runs for every join and must never spam errors.
            if role is not None and role < member.guild.me.top_role:
                try:
                    await member.add_roles(role, reason="Autorole")
                except discord.HTTPException:
                    pass

        await self._announce(
            member,
            config.get("welcome_channel_id"),
            config.get("welcome_message") or DEFAULT_WELCOME,
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        config = await self.bot.db.get_guild_config(member.guild.id)
        await self._announce(
            member,
            config.get("leave_channel_id"),
            config.get("leave_message") or DEFAULT_LEAVE,
        )

    async def _announce(self, member: discord.Member, channel_id: int | None, template: str) -> None:
        if not channel_id:
            return
        channel = member.guild.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return
        try:
            await channel.send(
                render(template, member),
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            )
        except discord.HTTPException:
            pass

    # ── configuration ────────────────────────────────────────────────────────

    @commands.hybrid_group(name="welcome", fallback="show", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def welcome(self, ctx: commands.Context) -> None:
        """Configure join messages."""
        config = await self.bot.db.get_guild_config(ctx.guild.id)
        channel_id = config.get("welcome_channel_id")
        embed = embeds.info(
            f"**Channel:** {f'<#{channel_id}>' if channel_id else '*disabled*'}\n"
            f"**Message:** {config.get('welcome_message') or DEFAULT_WELCOME}",
            title="Welcome settings",
        )
        embed.set_footer(text="Placeholders: {user} {user.name} {server} {count}")
        await ctx.send(embed=embed)

    @welcome.command(name="channel")
    @app_commands.describe(channel="Where to post join messages. Omit to turn them off.")
    async def welcome_channel(
        self, ctx: commands.Context, channel: discord.TextChannel | None = None
    ) -> None:
        """Set (or clear) the welcome channel."""
        await self.bot.db.set_guild_setting(
            ctx.guild.id, "welcome_channel_id", channel.id if channel else None
        )
        if channel:
            await ctx.send(embed=embeds.success(f"Welcome messages will go to {channel.mention}."))
        else:
            await ctx.send(embed=embeds.success("Welcome messages disabled."))

    @welcome.command(name="message")
    @app_commands.describe(message="Template to post. See /welcome placeholders.")
    async def welcome_message(self, ctx: commands.Context, *, message: str) -> None:
        """Set the welcome message."""
        if len(message) > 1500:
            raise FriendlyError("Keep the welcome message under 1500 characters.")
        await self.bot.db.set_guild_setting(ctx.guild.id, "welcome_message", message)
        await ctx.send(
            embed=embeds.success(f"Welcome message set. Preview:\n\n{render(message, ctx.author)}")
        )

    @welcome.command(name="test")
    async def welcome_test(self, ctx: commands.Context) -> None:
        """Fire the welcome flow against yourself."""
        config = await self.bot.db.get_guild_config(ctx.guild.id)
        if not config.get("welcome_channel_id"):
            raise FriendlyError("No welcome channel is set.")
        await self._announce(
            ctx.author, config["welcome_channel_id"], config.get("welcome_message") or DEFAULT_WELCOME
        )
        await ctx.send(embed=embeds.success("Sent a test welcome."), ephemeral=True)

    @welcome.command(name="placeholders")
    async def welcome_placeholders(self, ctx: commands.Context) -> None:
        """List the placeholders you can use."""
        await ctx.send(embed=embeds.info(PLACEHOLDERS, title="Placeholders"))

    @commands.hybrid_group(name="goodbye", aliases=["leavemsg"], fallback="show", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def goodbye(self, ctx: commands.Context) -> None:
        """Configure leave messages."""
        config = await self.bot.db.get_guild_config(ctx.guild.id)
        channel_id = config.get("leave_channel_id")
        await ctx.send(
            embed=embeds.info(
                f"**Channel:** {f'<#{channel_id}>' if channel_id else '*disabled*'}\n"
                f"**Message:** {config.get('leave_message') or DEFAULT_LEAVE}",
                title="Leave settings",
            )
        )

    @goodbye.command(name="channel")
    @app_commands.describe(channel="Where to post leave messages. Omit to turn them off.")
    async def goodbye_channel(
        self, ctx: commands.Context, channel: discord.TextChannel | None = None
    ) -> None:
        """Set (or clear) the leave channel."""
        await self.bot.db.set_guild_setting(
            ctx.guild.id, "leave_channel_id", channel.id if channel else None
        )
        if channel:
            await ctx.send(embed=embeds.success(f"Leave messages will go to {channel.mention}."))
        else:
            await ctx.send(embed=embeds.success("Leave messages disabled."))

    @goodbye.command(name="message")
    @app_commands.describe(message="Template to post")
    async def goodbye_message(self, ctx: commands.Context, *, message: str) -> None:
        """Set the leave message."""
        if len(message) > 1500:
            raise FriendlyError("Keep the leave message under 1500 characters.")
        await self.bot.db.set_guild_setting(ctx.guild.id, "leave_message", message)
        await ctx.send(embed=embeds.success("Leave message set."))

    @commands.hybrid_command(name="autorole")
    @app_commands.describe(role="Role to give new members. Omit to turn it off.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def autorole(self, ctx: commands.Context, role: discord.Role | None = None) -> None:
        """Automatically give a role to everyone who joins."""
        if role is not None:
            if role >= ctx.guild.me.top_role:
                raise FriendlyError(
                    f"**{role.name}** is above my highest role, so I can't assign it. "
                    "Move my role higher in Server Settings → Roles."
                )
            if role.is_default() or role.managed:
                raise FriendlyError("That role can't be assigned by a bot.")

        await self.bot.db.set_guild_setting(ctx.guild.id, "autorole_id", role.id if role else None)
        if role:
            await ctx.send(embed=embeds.success(f"New members will get **{role.name}**."))
        else:
            await ctx.send(embed=embeds.success("Autorole disabled."))


async def setup(bot) -> None:
    await bot.add_cog(Welcome(bot))
