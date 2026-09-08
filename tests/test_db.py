import os
import tempfile
import unittest

from bot.db import Database


class TestDatabase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "test.db"))
        await self.db.connect()
        await self.db.migrate()

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_migrations_are_idempotent(self):
        """Every restart re-runs migrate(); it must be a no-op the second time."""
        await self.db.migrate()
        applied = await self.db.fetchall("SELECT version FROM schema_migrations")
        self.assertEqual(len(applied), len(set(row["version"] for row in applied)))

    async def test_wal_mode_is_enabled(self):
        mode = await self.db.fetchval("PRAGMA journal_mode")
        self.assertEqual(mode.lower(), "wal")

    async def test_guild_config_is_created_on_first_read(self):
        config = await self.db.get_guild_config(1)
        self.assertEqual(config["guild_id"], 1)
        self.assertIsNone(config["prefix"])
        self.assertEqual(config["xp_min"], 15)

    async def test_setting_a_value_invalidates_the_cache(self):
        await self.db.get_guild_config(1)
        await self.db.set_guild_setting(1, "prefix", "?")
        self.assertEqual((await self.db.get_guild_config(1))["prefix"], "?")

    async def test_unknown_settings_are_rejected(self):
        """The setter interpolates the column name, so the whitelist is load-bearing."""
        with self.assertRaises(KeyError):
            await self.db.set_guild_setting(1, "prefix = 'x'; DROP TABLE tags; --", "boom")
        self.assertIsNotNone(await self.db.fetchone("SELECT name FROM sqlite_master WHERE name = 'tags'"))

    async def test_deleting_a_tag_cascades_to_its_aliases(self):
        cursor = await self.db.execute(
            "INSERT INTO tags (guild_id, name, content, owner_id, created_at, updated_at)"
            " VALUES (1, 'hi', 'hello', 1, 0, 0)"
        )
        await self.db.execute(
            "INSERT INTO tag_aliases (guild_id, alias, tag_id) VALUES (1, 'hey', ?)", (cursor.lastrowid,)
        )
        await self.db.execute("DELETE FROM tags WHERE id = ?", (cursor.lastrowid,))
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM tag_aliases"), 0)

    async def test_deleting_a_playlist_cascades_to_its_tracks(self):
        cursor = await self.db.execute(
            "INSERT INTO playlists (guild_id, owner_id, name, created_at) VALUES (1, 1, 'mix', 0)"
        )
        await self.db.execute(
            "INSERT INTO playlist_tracks (playlist_id, position, title, url) VALUES (?, 0, 't', 'u')",
            (cursor.lastrowid,),
        )
        await self.db.execute("DELETE FROM playlists WHERE id = ?", (cursor.lastrowid,))
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM playlist_tracks"), 0)

    async def test_tag_names_are_unique_per_guild(self):
        import aiosqlite

        await self.db.execute(
            "INSERT INTO tags (guild_id, name, content, owner_id, created_at, updated_at)"
            " VALUES (1, 'hi', 'a', 1, 0, 0)"
        )
        with self.assertRaises(aiosqlite.IntegrityError):
            await self.db.execute(
                "INSERT INTO tags (guild_id, name, content, owner_id, created_at, updated_at)"
                " VALUES (1, 'hi', 'b', 1, 0, 0)"
            )
        # A different guild may reuse the name.
        await self.db.execute(
            "INSERT INTO tags (guild_id, name, content, owner_id, created_at, updated_at)"
            " VALUES (2, 'hi', 'b', 1, 0, 0)"
        )

    async def test_xp_upsert_accumulates(self):
        for _ in range(3):
            await self.db.execute(
                "INSERT INTO levels (guild_id, user_id, xp, messages, last_xp_at) VALUES (1, 1, 10, 1, 0)"
                " ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = xp + 10, messages = messages + 1",
            )
        row = await self.db.fetchone("SELECT xp, messages FROM levels WHERE guild_id = 1 AND user_id = 1")
        self.assertEqual((row["xp"], row["messages"]), (30, 3))


class TestSchemaUpgrade(unittest.IsolatedAsyncioTestCase):
    """A bot that was already running must pick up new migrations cleanly."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "old.db"))
        await self.db.connect()

        # Recreate the state of a database created before the Red importer
        # existed: migration 001 applied, 002 not yet.
        from bot.db import MIGRATIONS_DIR

        await self.db.conn.executescript((MIGRATIONS_DIR / "001_init.sql").read_text())
        await self.db.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY, applied_at REAL NOT NULL)"
        )
        await self.db.conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES ('001_init.sql', 0)"
        )
        await self.db.conn.commit()
        await self.db.execute(
            "INSERT INTO tags (guild_id, name, content, owner_id, created_at, updated_at)"
            " VALUES (1, 'old', 'still here', 1, 0, 0)"
        )

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_pending_migration_is_applied_on_startup(self):
        await self.db.migrate()
        applied = {row["version"] for row in await self.db.fetchall("SELECT version FROM schema_migrations")}
        self.assertIn("002_tag_random.sql", applied)

    async def test_existing_tags_survive_and_default_to_not_random(self):
        await self.db.migrate()
        row = await self.db.fetchone("SELECT content, is_random FROM tags WHERE name = 'old'")
        self.assertEqual(row["content"], "still here")
        self.assertEqual(row["is_random"], 0)

    async def test_upgrade_is_idempotent(self):
        await self.db.migrate()
        await self.db.migrate()
        self.assertEqual(
            await self.db.fetchval(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = '002_tag_random.sql'"
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
