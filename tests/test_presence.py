"""The owner-only `status` command and the presence it restores at startup."""

import asyncio
import os
import tempfile
import unittest

import discord

os.environ.setdefault("DISCORD_TOKEN", "offline-test-token")

from bot.bot import MusicBot  # noqa: E402
from bot.config import load_config  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.utils import presence  # noqa: E402


class TestBuildActivity(unittest.TestCase):
    def test_each_type_round_trips_through_discord(self):
        """to_dict/create_activity is what a reconnect puts the presence through."""
        for kind in ("playing", "listening", "watching", "competing", "custom"):
            with self.subTest(kind=kind):
                activity = presence.build_activity(kind, "the queue")
                restored = discord.activity.create_activity(activity.to_dict(), None)
                self.assertEqual(restored.name, "the queue")

    def test_streaming_keeps_its_url(self):
        activity = presence.build_activity("streaming", "tunes", "https://twitch.tv/example")
        self.assertEqual(activity.url, "https://twitch.tv/example")
        self.assertIn("twitch.tv/example", presence.describe(activity))

    def test_custom_shows_the_text_with_no_verb(self):
        self.assertEqual(presence.describe(presence.build_activity("custom", "vibing")), "**vibing**")

    def test_listening_reads_the_way_discord_renders_it(self):
        self.assertEqual(
            presence.describe(presence.build_activity("listening", "music")), "Listening to **music**"
        )

    def test_no_activity_and_empty_text_mean_nothing(self):
        for kind, name in ((presence.NO_ACTIVITY, "ignored"), ("watching", ""), (None, "x"), ("bogus", "x")):
            with self.subTest(kind=kind, name=name):
                self.assertIsNone(presence.build_activity(kind, name))
        self.assertEqual(presence.describe(None), "*nothing*")

    def test_every_status_has_a_label(self):
        for key, status in presence.STATUSES.items():
            with self.subTest(status=key):
                self.assertNotEqual(presence.describe_status(status), status.value)


class TestConfigActivity(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_a_misspelled_activity_type_falls_back_instead_of_crashing(self):
        os.environ["ACTIVITY_TYPE"] = "vibing"
        os.environ["ACTIVITY_NAME"] = "music"
        self.assertEqual(presence.describe(load_config().activity), "Listening to **music**")

    def test_no_activity_name_means_no_activity(self):
        os.environ["ACTIVITY_NAME"] = ""
        self.assertIsNone(load_config().activity)


class TestBotSettings(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "test.db"))
        await self.db.connect()
        await self.db.migrate()

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_settings_start_empty(self):
        self.assertEqual(await self.db.get_bot_settings(), {})

    async def test_writes_are_visible_through_the_cache(self):
        await self.db.get_bot_settings()  # warm the cache
        await self.db.set_bot_settings({"activity_type": "watching", "activity_name": "the queue"})
        settings = await self.db.get_bot_settings()
        self.assertEqual(settings["activity_type"], "watching")
        self.assertEqual(settings["activity_name"], "the queue")

    async def test_writing_the_same_key_twice_updates_it(self):
        await self.db.set_bot_settings({"activity_name": "first"})
        await self.db.set_bot_settings({"activity_name": "second"})
        self.assertEqual((await self.db.get_bot_settings())["activity_name"], "second")
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM bot_settings"), 1)

    async def test_nulls_round_trip(self):
        """`status clear` stores a type with no name."""
        await self.db.set_bot_settings({"activity_type": "none", "activity_name": None})
        self.assertIsNone((await self.db.get_bot_settings())["activity_name"])

    async def test_unknown_keys_are_rejected(self):
        with self.assertRaises(KeyError):
            await self.db.set_bot_settings({"token": "hunter2"})
        self.assertEqual(await self.db.get_bot_settings(), {})

    async def test_the_caller_cannot_mutate_the_cache(self):
        await self.db.set_bot_settings({"activity_name": "mine"})
        (await self.db.get_bot_settings())["activity_name"] = "tampered"
        self.assertEqual((await self.db.get_bot_settings())["activity_name"], "mine")


class FakeContext:
    """Just enough Context for a command body that only sends an embed."""

    def __init__(self):
        self.sent = []

    async def send(self, *, embed=None, **kwargs):
        self.sent.append(embed)


class TestStatusCommand(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._env = dict(os.environ)
        os.environ["DATABASE_PATH"] = os.path.join(self._dir.name, "bot.db")
        os.environ["DEV_GUILD_IDS"] = ""
        os.environ["ACTIVITY_TYPE"] = "listening"
        os.environ["ACTIVITY_NAME"] = "music with friends"
        os.environ["OWNER_IDS"] = "42"  # so is_owner answers offline

        self.bot = await self._boot()
        self.cog = self.bot.get_cog("Admin")
        self.ctx = FakeContext()

    async def asyncTearDown(self):
        await self.bot.db.close()
        os.environ.clear()
        os.environ.update(self._env)
        self._dir.cleanup()

    async def _boot(self) -> MusicBot:
        """Start a bot against the same database, as a restart would."""
        bot = MusicBot(load_config())
        bot._ready = asyncio.Event()
        # There is no gateway offline, so record what would have been pushed.
        bot.pushed = []

        async def change_presence(*, activity=None, status=None):
            bot.pushed.append((activity, status))

        bot.change_presence = change_presence
        await bot.db.connect()
        await bot.db.migrate()
        await bot._restore_presence()
        await bot.load_extension("bot.cogs.admin")
        return bot

    async def test_env_activity_is_used_when_nothing_is_saved(self):
        self.assertEqual(presence.describe(self.bot.activity), "Listening to **music with friends**")

    async def test_setting_an_activity_pushes_and_persists_it(self):
        await self.cog.status_set(self.ctx, kind="watching", text="  the queue  ")
        self.assertEqual(presence.describe(self.bot.activity), "Watching **the queue**")
        self.assertEqual(presence.describe(self.bot.pushed[-1][0]), "Watching **the queue**")

        saved = await self.bot.db.get_bot_settings()
        self.assertEqual(saved["activity_type"], "watching")
        self.assertEqual(saved["activity_name"], "the queue")

    async def test_a_saved_activity_survives_a_restart(self):
        await self.cog.status_set(self.ctx, kind="playing", text="with the API")
        await self.bot.db.close()

        self.bot = await self._boot()
        self.assertEqual(presence.describe(self.bot.activity), "Playing **with the API**")

    async def test_changing_the_dot_keeps_the_activity(self):
        """change_presence replaces the whole presence, so the other half must be resent."""
        await self.cog.status_set(self.ctx, kind="watching", text="the queue")
        await self.cog.status_presence(self.ctx, state="dnd")

        activity, status = self.bot.pushed[-1]
        self.assertEqual(presence.describe(activity), "Watching **the queue**")
        self.assertEqual(status, discord.Status.dnd)
        self.assertEqual(self.bot.status, discord.Status.dnd)

    async def test_changing_the_activity_keeps_the_dot(self):
        await self.cog.status_presence(self.ctx, state="idle")
        await self.cog.status_set(self.ctx, kind="playing", text="something else")
        self.assertEqual(self.bot.pushed[-1][1], discord.Status.idle)

    async def test_a_saved_dot_survives_a_restart_without_touching_the_env_activity(self):
        await self.cog.status_presence(self.ctx, state="idle")
        await self.bot.db.close()

        self.bot = await self._boot()
        self.assertEqual(self.bot.status, discord.Status.idle)
        self.assertEqual(presence.describe(self.bot.activity), "Listening to **music with friends**")

    async def test_clear_removes_the_activity_for_good(self):
        await self.cog.status_set(self.ctx, kind="playing", text="something")
        await self.cog.status_clear(self.ctx)
        self.assertIsNone(self.bot.activity)

        await self.bot.db.close()
        self.bot = await self._boot()
        self.assertIsNone(self.bot.activity, "cleared status came back from .env after a restart")

    async def test_streaming_needs_a_real_link(self):
        from bot.utils.errors import FriendlyError

        with self.assertRaises(FriendlyError):
            await self.cog.status_streaming(self.ctx, url="twitch.tv/example", text="tunes")

        await self.cog.status_streaming(self.ctx, url="https://twitch.tv/example", text="tunes")
        self.assertEqual(self.bot.activity.url, "https://twitch.tv/example")

    async def test_overlong_text_is_refused_before_discord_sees_it(self):
        from bot.utils.errors import FriendlyError

        with self.assertRaises(FriendlyError):
            await self.cog.status_set(self.ctx, kind="playing", text="x" * 129)
        with self.assertRaises(FriendlyError):
            await self.cog.status_set(self.ctx, kind="playing", text="   ")
        self.assertEqual(self.bot.pushed, [])

    async def test_show_reports_the_current_presence(self):
        await self.cog.status_set(self.ctx, kind="competing", text="a dance-off")
        await self.cog.status_presence(self.ctx, state="invisible")
        description = self.cog._presence_description()
        self.assertIn("Competing in **a dance-off**", description)
        self.assertIn("Invisible", description)

    async def test_every_subcommand_is_owner_only(self):
        """A group's checks don't run for prefix invocations of its children,
        so each subcommand has to carry `is_owner` itself."""
        from discord.ext import commands

        group = self.bot.get_command("status")
        stranger = _NotTheOwner(self.bot)
        for command in (group, *group.commands):
            with self.subTest(command=command.qualified_name):
                self.assertTrue(command.checks, f"{command.qualified_name} has no checks")
                for check in command.checks:
                    with self.assertRaises(commands.NotOwner):
                        await check(stranger)


class _NotTheOwner:
    """A context whose author is nobody in particular."""

    def __init__(self, bot):
        self.bot = bot
        self.author = type("User", (), {"id": 1})()


if __name__ == "__main__":
    unittest.main()
