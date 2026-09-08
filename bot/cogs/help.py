"""A `/help` that works as both a slash command and a text command."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..utils import embeds
from ..utils.errors import FriendlyError
from ..utils.formatting import truncate

COG_EMOJI = {
    "Music": "🎵",
    "Tags": "💬",
    "Moderation": "🛡️",
    "Welcome": "👋",
    "Roles": "🎭",
    "Fun": "🎲",
    "Utility": "🔧",
    "Leveling": "📈",
    "Admin": "⚙️",
}

HIDDEN_COGS = {"Help"}


class Help(commands.Cog):
    """How to use everything."""

    def __init__(self, bot) -> None:
        self.bot = bot

    def _visible_commands(self, cog: commands.Cog) -> list[commands.Command]:
        return sorted(
            (command for command in cog.get_commands() if not command.hidden),
            key=lambda command: command.qualified_name,
        )

    @commands.hybrid_command(name="help", aliases=["commands", "h"])
    @app_commands.describe(query="A command or category to explain. Omit for an overview.")
    async def help(self, ctx: commands.Context, *, query: str | None = None) -> None:
        """Show what the bot can do."""
        prefix = ctx.clean_prefix
        if query is None:
            await ctx.send(embed=self._overview(prefix))
            return

        query = query.strip().lstrip("/").lower()

        command = self.bot.get_command(query)
        if command is not None and not command.hidden:
            await ctx.send(embed=self._command_embed(command, prefix))
            return

        for cog_name, cog in self.bot.cogs.items():
            if cog_name.lower() == query and cog_name not in HIDDEN_COGS:
                await ctx.send(embed=self._cog_embed(cog, prefix))
                return

        raise FriendlyError(f"No command or category called **{truncate(query, 40)}**.")

    def _overview(self, prefix: str) -> discord.Embed:
        embed = embeds.info(
            f"Every command works two ways: as a slash command (`/play`) or with the "
            f"prefix (`{prefix}play`). Mentioning me works as a prefix too.\n\n"
            f"Use `{prefix}help <command>` for details on one command, or "
            f"`{prefix}help <category>` to list a whole category.",
            title=f"{self.bot.user.name} — help",
        )
        for cog_name, cog in self.bot.cogs.items():
            if cog_name in HIDDEN_COGS:
                continue
            commands_list = self._visible_commands(cog)
            if not commands_list:
                continue
            names = " ".join(f"`{command.name}`" for command in commands_list)
            embed.add_field(
                name=f"{COG_EMOJI.get(cog_name, '•')} {cog_name}",
                value=truncate(names, 1000),
                inline=False,
            )
        embed.set_footer(text="Custom commands made on this server: /tag all")
        return embed

    def _cog_embed(self, cog: commands.Cog, prefix: str) -> discord.Embed:
        lines = [
            f"**{prefix}{command.qualified_name}** — {command.short_doc or 'No description.'}"
            for command in self._visible_commands(cog)
        ]
        return embeds.info(
            (cog.description or "") + "\n\n" + "\n".join(lines),
            title=f"{COG_EMOJI.get(cog.qualified_name, '•')} {cog.qualified_name}",
        )

    def _command_embed(self, command: commands.Command, prefix: str) -> discord.Embed:
        embed = embeds.info(
            command.help or command.short_doc or "No description.",
            title=f"{prefix}{command.qualified_name} {command.signature}".strip(),
        )
        if command.aliases:
            embed.add_field(
                name="Aliases", value=", ".join(f"`{alias}`" for alias in command.aliases), inline=False
            )
        if isinstance(command, commands.Group):
            subcommands = [sub for sub in command.commands if not sub.hidden]
            if subcommands:
                embed.add_field(
                    name="Subcommands",
                    value="\n".join(
                        f"**{prefix}{sub.qualified_name}** — {sub.short_doc or 'No description.'}"
                        for sub in sorted(subcommands, key=lambda sub: sub.name)
                    ),
                    inline=False,
                )
        embed.set_footer(text="<required>  [optional]")
        return embed


async def setup(bot) -> None:
    await bot.add_cog(Help(bot))
