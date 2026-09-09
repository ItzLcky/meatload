"""The per-server `config cleanup` setting and the deletion it drives."""

import asyncio
import os
import tempfile
import unittest

import discord

os.environ.setdefault("DISCORD_TOKEN", "offline-test-token")

from bot.bot import MusicBot  # noqa: E402
from bot.config import load_config  # noqa: E402


class FakeMessage:
    def __init__(self, raises: Exception | None = None):
        self.deleted = False
        self._raises = raises

    async def delete(self):
        if self._raises is not None:
            raise self._raises
        self.deleted = True


class FakeGuild:
    def __init__(self, guild_id: int = 1, manage_messages: bool = True):
        self.id = guild_id
        self.me = type(
            "FakeMember", (), {"guild_permissions": discord.Permissions(manage_messages=manage_messages)}
        )()


_DEFAULT = object()


class FakeContext:
    """Just enough Context for the cleanup path and the command that sets it."""

    def __init__(self, *, guild=_DEFAULT, interaction=None, message=None):
        self.guild = FakeGuild() if guild is _DEFAULT else guild
        self.interaction = interaction
        self.message = message if message is not None else FakeMessage()
        self.command = "example"
        self.sent = []

    async def send(self, *, embed=None, **kwargs):
        self.sent.append(embed)


class TestCommandCleanup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._env = dict(os.environ)
        os.environ["DATABASE_PATH"] = os.path.join(self._dir.name, "bot.db")
        os.environ["DEV_GUILD_IDS"] = ""

        self.bot = MusicBot(load_config())
        self.bot._ready = asyncio.Event()
        await self.bot.db.connect()
        await self.bot.db.migrate()
        await self.bot.load_extension("bot.cogs.admin")
        self.cog = self.bot.get_cog("Admin")

    async def asyncTearDown(self):
        await self.bot.db.close()
        os.environ.clear()
        os.environ.update(self._env)
        self._dir.cleanup()

    async def _enable(self, guild_id: int = 1):
        await self.bot.db.set_guild_setting(guild_id, "delete_command_messages", 1)

    # ── the setting ──────────────────────────────────────────────────────────

    async def test_it_is_off_until_a_server_turns_it_on(self):
        config = await self.bot.db.get_guild_config(1)
        self.assertEqual(config["delete_command_messages"], 0)

        ctx = FakeContext()
        await self.bot.cleanup_invocation(ctx)
        self.assertFalse(ctx.message.deleted)

    async def test_the_command_persists_both_states(self):
        ctx = FakeContext()
        await self.cog.config_cleanup(ctx, state="on")
        self.assertEqual((await self.bot.db.get_guild_config(1))["delete_command_messages"], 1)

        await self.cog.config_cleanup(ctx, state="off")
        self.assertEqual((await self.bot.db.get_guild_config(1))["delete_command_messages"], 0)

    async def test_turning_it_on_without_manage_messages_warns(self):
        ctx = FakeContext(guild=FakeGuild(manage_messages=False))
        await self.cog.config_cleanup(ctx, state="on")
        self.assertIn("Manage Messages", ctx.sent[-1].description)
        # Still saved: the permission can be granted afterwards.
        self.assertEqual((await self.bot.db.get_guild_config(1))["delete_command_messages"], 1)

    # ── the deletion ─────────────────────────────────────────────────────────

    async def test_an_enabled_server_gets_its_invocation_deleted(self):
        await self._enable()
        ctx = FakeContext()
        await self.bot.cleanup_invocation(ctx)
        self.assertTrue(ctx.message.deleted)

    async def test_slash_commands_are_left_alone(self):
        """There is no invoking message to delete, and ctx.message is someone else's."""
        await self._enable()
        ctx = FakeContext(interaction=object())
        await self.bot.cleanup_invocation(ctx)
        self.assertFalse(ctx.message.deleted)

    async def test_dms_are_left_alone(self):
        ctx = FakeContext(guild=None)
        await self.bot.cleanup_invocation(ctx)
        self.assertFalse(ctx.message.deleted)

    async def test_a_refused_delete_does_not_escape(self):
        """Missing Manage Messages must not turn a command that worked into an error."""
        await self._enable()
        forbidden = discord.Forbidden(
            type("FakeResponse", (), {"status": 403, "reason": "Forbidden"})(), "missing permissions"
        )
        ctx = FakeContext(message=FakeMessage(raises=forbidden))
        await self.bot.cleanup_invocation(ctx)  # must not raise

    async def test_completion_hook_routes_through_the_setting(self):
        await self._enable()
        ctx = FakeContext()
        await self.bot.on_command_completion(ctx)
        self.assertTrue(ctx.message.deleted)


if __name__ == "__main__":
    unittest.main()
