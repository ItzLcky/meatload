"""Owner-only maintenance plus per-server settings."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..utils import embeds
from ..utils.errors import FriendlyError

log = logging.getLogger(__name__)

COGS_HINT = "music, tags, moderation, welcome, roles, fun, utility, leveling, admin, help"


class Admin(commands.Cog):
    """Bot maintenance and server configuration."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # ── owner only ───────────────────────────────────────────────────────────
    # These stay prefix-only on purpose: `sync` is what *creates* slash
    # commands, so it can't depend on them existing.

    @commands.command(name="sync")
    @commands.is_owner()
    async def sync(self, ctx: commands.Context, scope: str = "guild") -> None:
        """Push slash commands to Discord. `sync guild` | `global` | `clear`."""
        async with ctx.typing():
            if scope == "global":
                synced = await self.bot.tree.sync()
                await ctx.send(
                    embed=embeds.success(
                        f"Synced **{len(synced)}** commands globally. "
                        "Global commands can take up to an hour to appear."
                    )
                )
            elif scope == "clear":
                if ctx.guild is None:
                    raise FriendlyError("Run `clear` inside a server.")
                self.bot.tree.clear_commands(guild=ctx.guild)
                await self.bot.tree.sync(guild=ctx.guild)
                await ctx.send(embed=embeds.success("Cleared this server's slash commands."))
            else:
                if ctx.guild is None:
                    raise FriendlyError("Run this inside a server, or use `sync global`.")
                self.bot.tree.copy_global_to(guild=ctx.guild)
                synced = await self.bot.tree.sync(guild=ctx.guild)
                await ctx.send(
                    embed=embeds.success(f"Synced **{len(synced)}** commands to **{ctx.guild.name}**.")
                )

    @commands.command(name="reload")
    @commands.is_owner()
    async def reload(self, ctx: commands.Context, cog: str) -> None:
        """Reload a cog without restarting. Useful while editing code."""
        target = cog if cog.startswith("bot.cogs.") else f"bot.cogs.{cog}"
        try:
            await self.bot.reload_extension(target)
        except commands.ExtensionNotLoaded:
            raise FriendlyError(f"`{cog}` isn't loaded. Available: {COGS_HINT}")
        except commands.ExtensionError as exc:
            raise FriendlyError(f"Reloading `{cog}` failed:\n```\n{exc}\n```")
        await ctx.send(embed=embeds.success(f"Reloaded `{cog}`."))

    @commands.command(name="load")
    @commands.is_owner()
    async def load(self, ctx: commands.Context, cog: str) -> None:
        """Load a cog."""
        try:
            await self.bot.load_extension(f"bot.cogs.{cog}")
        except commands.ExtensionError as exc:
            raise FriendlyError(f"Loading `{cog}` failed:\n```\n{exc}\n```")
        await ctx.send(embed=embeds.success(f"Loaded `{cog}`."))

    @commands.command(name="unload")
    @commands.is_owner()
    async def unload(self, ctx: commands.Context, cog: str) -> None:
        """Unload a cog."""
        if cog == "admin":
            raise FriendlyError("Unloading admin would leave no way to load it back.")
        try:
            await self.bot.unload_extension(f"bot.cogs.{cog}")
        except commands.ExtensionError as exc:
            raise FriendlyError(f"Unloading `{cog}` failed:\n```\n{exc}\n```")
        await ctx.send(embed=embeds.success(f"Unloaded `{cog}`."))

    @commands.command(name="shutdown", aliases=["die", "restart"])
    @commands.is_owner()
    async def shutdown(self, ctx: commands.Context) -> None:
        """Disconnect cleanly. Docker's `restart: unless-stopped` brings it back."""
        await ctx.send(embed=embeds.neutral("Shutting down. 👋"))
        await self.bot.close()

    # ── server settings ──────────────────────────────────────────────────────

    @commands.hybrid_group(name="config", aliases=["settings"], fallback="show", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def config(self, ctx: commands.Context) -> None:
        """Show this server's settings."""
        settings = await self.bot.db.get_guild_config(ctx.guild.id)

        def channel(key: str) -> str:
            value = settings.get(key)
            return f"<#{value}>" if value else "*not set*"

        def role(key: str) -> str:
            value = settings.get(key)
            return f"<@&{value}>" if value else "*not set*"

        embed = embeds.info(
            f"**Prefix:** `{settings.get('prefix') or self.bot.config.default_prefix}`\n"
            f"**Mod log:** {channel('modlog_channel_id')}\n"
            f"**DJ role:** {role('dj_role_id')}\n"
            f"**Tag manager role:** {role('tag_manager_role_id')}\n"
            f"**Autorole:** {role('autorole_id')}\n"
            f"**Music volume:** {settings.get('music_volume') or self.bot.config.music_default_volume}%\n"
            f"**Leveling:** {'on' if settings.get('leveling_enabled') else 'off'}",
            title=f"Settings for {ctx.guild.name}",
        )
        embed.set_footer(text="Welcome messages: /welcome • Leveling: /leveling")
        await ctx.send(embed=embed)

    @config.command(name="prefix")
    @app_commands.describe(prefix="New prefix, up to 5 characters. Omit to reset to the default.")
    async def config_prefix(self, ctx: commands.Context, prefix: str | None = None) -> None:
        """Change the text-command prefix for this server."""
        if prefix is not None:
            prefix = prefix.strip()
            if not prefix or len(prefix) > 5:
                raise FriendlyError("Prefixes must be 1-5 characters.")
        await self.bot.db.set_guild_setting(ctx.guild.id, "prefix", prefix)
        shown = prefix or self.bot.config.default_prefix
        await ctx.send(
            embed=embeds.success(
                f"Prefix set to `{shown}`. Mentioning me always works too, and every "
                "command is also available as a slash command."
            )
        )

    @config.command(name="modlog")
    @app_commands.describe(channel="Where to post moderation cases. Omit to turn it off.")
    async def config_modlog(
        self, ctx: commands.Context, channel: discord.TextChannel | None = None
    ) -> None:
        """Set the mod-log channel."""
        if channel is not None and not channel.permissions_for(ctx.guild.me).send_messages:
            raise FriendlyError(f"I can't send messages in {channel.mention}.")
        await self.bot.db.set_guild_setting(
            ctx.guild.id, "modlog_channel_id", channel.id if channel else None
        )
        await ctx.send(
            embed=embeds.success(
                f"Mod log set to {channel.mention}." if channel else "Mod log disabled."
            )
        )

    @config.command(name="djrole")
    @app_commands.describe(role="Role required to control music while others listen. Omit to remove.")
    async def config_djrole(self, ctx: commands.Context, role: discord.Role | None = None) -> None:
        """Restrict disruptive music commands to a role."""
        await self.bot.db.set_guild_setting(ctx.guild.id, "dj_role_id", role.id if role else None)
        if role:
            await ctx.send(
                embed=embeds.success(
                    f"**{role.name}** is now the DJ role. Anyone can still queue music and "
                    "control the player when they're alone in the channel."
                )
            )
        else:
            await ctx.send(embed=embeds.success("DJ role removed — everyone can control music."))

    @config.command(name="tagrole")
    @app_commands.describe(role="Role allowed to manage custom commands. Omit to remove.")
    async def config_tagrole(self, ctx: commands.Context, role: discord.Role | None = None) -> None:
        """Let a role create custom commands without Manage Messages."""
        await self.bot.db.set_guild_setting(ctx.guild.id, "tag_manager_role_id", role.id if role else None)
        if role:
            await ctx.send(embed=embeds.success(f"**{role.name}** can now manage custom commands."))
        else:
            await ctx.send(
                embed=embeds.success("Tag manager role removed — Manage Messages is required again.")
            )


async def setup(bot) -> None:
    await bot.add_cog(Admin(bot))
