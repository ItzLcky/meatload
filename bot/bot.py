"""The Bot subclass: wiring, prefix resolution, and error handling."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import discord
from discord.ext import commands, tasks

from .config import Config
from .db import Database
from .utils import embeds
from .utils.errors import FriendlyError

log = logging.getLogger(__name__)

EXTENSIONS = (
    "bot.cogs.admin",
    "bot.cogs.help",
    "bot.cogs.music",
    "bot.cogs.tags",
    "bot.cogs.moderation",
    "bot.cogs.welcome",
    "bot.cogs.roles",
    "bot.cogs.fun",
    "bot.cogs.utility",
    "bot.cogs.leveling",
)


async def _resolve_prefix(bot: "MusicBot", message: discord.Message) -> list[str]:
    prefix = bot.config.default_prefix
    if message.guild is not None:
        config = await bot.db.get_guild_config(message.guild.id)
        prefix = config.get("prefix") or prefix
    # A mention always works, which is the escape hatch if someone forgets
    # what the prefix was changed to.
    return commands.when_mentioned_or(prefix)(bot, message)


class MusicBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # prefix commands and tags
        intents.members = True  # welcome messages, autorole, userinfo
        intents.voice_states = True  # music

        super().__init__(
            command_prefix=_resolve_prefix,
            intents=intents,
            help_command=None,  # replaced by the Help cog, which supports slash
            case_insensitive=True,
            strip_after_prefix=True,
            owner_ids=config.owner_ids or None,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True, replied_user=False
            ),
            activity=config.activity,
        )
        self.config = config
        self.db = Database(config.database_path)
        self.started_at = time.time()

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.db.migrate()

        for extension in EXTENSIONS:
            try:
                await self.load_extension(extension)
                log.info("Loaded %s", extension)
            except Exception:
                # One broken cog shouldn't stop the rest of the bot from starting.
                log.exception("Failed to load %s", extension)

        for guild_id in self.config.dev_guild_ids:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d slash commands to guild %s", len(synced), guild_id)
            except discord.HTTPException:
                log.exception("Could not sync slash commands to guild %s", guild_id)

        if not self.config.dev_guild_ids:
            log.warning(
                "DEV_GUILD_IDS is empty, so slash commands are not being synced automatically. "
                "Set it in .env, or run `<prefix>sync global` once."
            )

        self.heartbeat.start()

    async def close(self) -> None:
        self.heartbeat.cancel()
        await super().close()
        await self.db.close()

    async def on_command_completion(self, ctx: commands.Context) -> None:
        await self.cleanup_invocation(ctx)

    async def cleanup_invocation(self, ctx: commands.Context) -> None:
        """Delete the message that ran a command, if the server asked for it.

        Only successful invocations are cleaned up. A command that failed keeps
        its message on screen, because the error underneath it ("you're missing
        the `member` argument") only reads as an answer next to the question.
        """
        if ctx.guild is None or ctx.interaction is not None:
            return  # slash commands leave no message behind to delete

        config = await self.db.get_guild_config(ctx.guild.id)
        if not config.get("delete_command_messages"):
            return

        try:
            await ctx.message.delete()
        except discord.HTTPException:
            # Missing Manage Messages, or something deleted it first. Either
            # way it isn't worth interrupting a command that already worked.
            log.debug("Could not clean up the invocation of %s", ctx.command, exc_info=True)

    async def on_ready(self) -> None:
        log.info(
            "Connected as %s (%s) in %d guild(s)",
            self.user,
            self.user.id,
            len(self.guilds),
        )

    @tasks.loop(seconds=30)
    async def heartbeat(self) -> None:
        """Touch a file the Docker healthcheck watches."""
        try:
            Path(self.config.heartbeat_path).write_text(str(time.time()))
        except OSError:
            log.debug("Could not write the heartbeat file", exc_info=True)

    @heartbeat.before_loop
    async def _before_heartbeat(self) -> None:
        await self.wait_until_ready()

    # ── error handling ───────────────────────────────────────────────────────

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        # A cog with its own handler has already dealt with it.
        if hasattr(ctx.command, "on_error"):
            return

        # Unwrap so a FriendlyError raised inside a command body still reads
        # as a friendly error rather than a crash.
        if isinstance(error, commands.CommandInvokeError) and isinstance(
            error.original, (FriendlyError, commands.CheckFailure, discord.Forbidden)
        ):
            error = error.original

        if isinstance(error, commands.CommandNotFound):
            tags = self.get_cog("Tags")
            if tags is not None and await tags.try_invoke_prefix_tag(ctx):
                # Tags never reach on_command_completion — they aren't real
                # commands — but to the person typing they are, so tidy up here.
                await self.cleanup_invocation(ctx)
                return
            return  # unknown prefix commands stay silent

        message: str | None = None

        if isinstance(error, FriendlyError):
            message = str(error)
        elif isinstance(error, commands.MissingRequiredArgument):
            message = (
                f"You're missing the `{error.param.name}` argument.\n"
                f"Usage: `{ctx.clean_prefix}{ctx.command.qualified_name} {ctx.command.signature}`"
            )
        elif isinstance(error, commands.MissingRequiredAttachment):
            # Subclass of UserInputError, so it has to be handled before it.
            message = (
                f"Attach the file to your message and run the command again, "
                f"or use the slash command `/{ctx.command.qualified_name}` for a file picker."
            )
        elif isinstance(error, commands.BadArgument | commands.UserInputError):
            message = (
                f"{error}\nUsage: `{ctx.clean_prefix}{ctx.command.qualified_name} "
                f"{ctx.command.signature}`"
            )
        elif isinstance(error, commands.NoPrivateMessage):
            message = "That command only works inside a server."
        elif isinstance(error, commands.MissingPermissions):
            missing = ", ".join(perm.replace("_", " ").title() for perm in error.missing_permissions)
            message = f"You need the **{missing}** permission for that."
        elif isinstance(error, commands.BotMissingPermissions):
            missing = ", ".join(perm.replace("_", " ").title() for perm in error.missing_permissions)
            message = f"I need the **{missing}** permission to do that."
        elif isinstance(error, commands.NotOwner):
            message = "That command is owner-only."
        elif isinstance(error, commands.CommandOnCooldown):
            message = f"Slow down — try again in {error.retry_after:.1f}s."
        elif isinstance(error, commands.CheckFailure):
            message = str(error) or "You can't use that here."
        elif isinstance(error, discord.Forbidden):
            message = "Discord refused that — I'm probably missing a permission or my role is too low."
        else:
            log.exception(
                "Unhandled error in command %s", ctx.command, exc_info=(type(error), error, error.__traceback__)
            )
            message = "Something went wrong on my end. The details are in the bot's logs."

        try:
            await ctx.send(embed=embeds.error(message), ephemeral=True)
        except discord.HTTPException:
            log.debug("Could not deliver the error message", exc_info=True)
