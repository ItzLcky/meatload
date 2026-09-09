"""Load every cog offline and validate the command tree.

This is the test that catches broken decorators, bad type annotations, and
slash-command payloads Discord would reject — without needing a token.
"""

import asyncio
import os
import tempfile
import unittest

os.environ.setdefault("DISCORD_TOKEN", "offline-test-token")

from bot.bot import EXTENSIONS, MusicBot  # noqa: E402
from bot.config import load_config  # noqa: E402


class TestExtensionLoading(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = os.path.join(self._dir.name, "bot.db")
        os.environ["DEV_GUILD_IDS"] = ""

        self.bot = MusicBot(load_config())
        # Background `tasks.loop`s call wait_until_ready(), which raises unless
        # the client has logged in. Give them an event that simply never fires.
        self.bot._ready = asyncio.Event()
        # `when_mentioned` reads bot.user, which normally only exists post-login.
        self.bot._connection.user = type("FakeUser", (), {"id": 12345})()

        await self.bot.db.connect()
        await self.bot.db.migrate()
        for extension in EXTENSIONS:
            await self.bot.load_extension(extension)

    async def asyncTearDown(self):
        for extension in reversed(EXTENSIONS):
            await self.bot.unload_extension(extension)
        await self.bot.db.close()
        self._dir.cleanup()

    async def test_every_extension_loaded(self):
        self.assertEqual(len(self.bot.extensions), len(EXTENSIONS))

    async def test_expected_commands_exist(self):
        names = {command.qualified_name for command in self.bot.walk_commands()}
        for expected in (
            "play", "skip", "queue", "tag create", "ban", "rank", "poll", "inspireme",
            "config prefix", "config cleanup", "status set",
            "balance", "pay", "daily", "culshi create", "culshi buy", "culshi resolve",
        ):
            with self.subTest(command=expected):
                self.assertIn(expected, names)

    async def test_red_custom_command_spellings_resolve(self):
        """Servers migrating from Red drive tags through `cc`, not `tag`."""
        expected = {
            "cc": "tag",
            "cc list": "tag all",
            "cc add": "tag create",
            "cc del": "tag delete",
            "cc edit": "tag edit",
            "cc random": "tag random",
            "customcom list": "tag all",
        }
        for typed, resolved in expected.items():
            with self.subTest(command=typed):
                command = self.bot.get_command(typed)
                self.assertIsNotNone(command, f"{typed} does not resolve")
                self.assertEqual(command.qualified_name, resolved)

    async def test_no_duplicate_command_names(self):
        seen = set()
        for command in self.bot.walk_commands():
            for name in (command.qualified_name, *(
                f"{command.full_parent_name} {alias}".strip() for alias in command.aliases
            )):
                with self.subTest(name=name):
                    self.assertNotIn(name, seen)
                seen.add(name)

    async def test_slash_payloads_are_valid(self):
        """to_dict is exactly what gets sent on sync, so it validates the tree."""
        for command in self.bot.tree.get_commands():
            with self.subTest(command=command.name):
                command.to_dict(self.bot.tree)

    async def test_within_discords_top_level_command_limit(self):
        self.assertLessEqual(len(self.bot.tree.get_commands()), 100)

    async def test_prefix_resolution_prefers_the_guild_setting(self):
        from bot.bot import _resolve_prefix

        class FakeGuild:
            id = 999

        class FakeMessage:
            guild = FakeGuild()
            content = ""

        message = FakeMessage()
        prefixes = await _resolve_prefix(self.bot, message)
        self.assertIn(self.bot.config.default_prefix, prefixes)

        await self.bot.db.set_guild_setting(999, "prefix", "?")
        prefixes = await _resolve_prefix(self.bot, message)
        self.assertIn("?", prefixes)
        self.assertNotIn(self.bot.config.default_prefix, prefixes)

    async def test_mention_always_works_as_a_prefix(self):
        """The escape hatch if someone forgets what they changed the prefix to."""
        from bot.bot import _resolve_prefix

        class FakeMessage:
            guild = None
            content = ""

        prefixes = await _resolve_prefix(self.bot, FakeMessage())
        self.assertTrue(any("12345" in prefix for prefix in prefixes))


if __name__ == "__main__":
    unittest.main()
