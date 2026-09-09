"""SQLite access layer.

One aiosqlite connection is used for the whole bot. aiosqlite runs every
statement on a single dedicated thread and serialises them internally, so a
shared connection is safe here and avoids pool bookkeeping. WAL mode keeps
reads from blocking behind the write of, say, an XP update.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

import aiosqlite

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Columns `/config` is allowed to write. Anything outside this set is rejected
# rather than interpolated into SQL.
GUILD_SETTINGS: frozenset[str] = frozenset(
    {
        "prefix",
        "modlog_channel_id",
        "welcome_channel_id",
        "welcome_message",
        "leave_channel_id",
        "leave_message",
        "autorole_id",
        "dj_role_id",
        "tag_manager_role_id",
        "delete_command_messages",
        "music_volume",
        "leveling_enabled",
        "levelup_channel_id",
        "levelup_message",
        "xp_min",
        "xp_max",
        "xp_cooldown",
    }
)

# Bot-wide settings, stored as key/value rows in `bot_settings`. Same idea as
# GUILD_SETTINGS: unknown keys are refused rather than written.
BOT_SETTINGS: frozenset[str] = frozenset(
    {
        "activity_type",
        "activity_name",
        "activity_url",
        "presence_status",
    }
)


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._guild_cache: dict[int, dict[str, Any]] = {}
        self._bot_cache: dict[str, str | None] | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() has not been awaited yet")
        return self._conn

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA synchronous = NORMAL")
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA busy_timeout = 5000")
        await self._conn.commit()
        log.info("Connected to database at %s", self.path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def migrate(self) -> None:
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version TEXT PRIMARY KEY,"
            "  applied_at REAL NOT NULL"
            ")"
        )
        await self.conn.commit()

        applied = {row["version"] for row in await self.fetchall("SELECT version FROM schema_migrations")}

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            log.info("Applying migration %s", path.name)
            await self.conn.executescript(path.read_text(encoding="utf-8"))
            await self.conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (path.name, time.time()),
            )
            await self.conn.commit()

    # ── primitives ───────────────────────────────────────────────────────────

    async def execute(self, query: str, params: Sequence[Any] = ()) -> aiosqlite.Cursor:
        cursor = await self.conn.execute(query, params)
        await self.conn.commit()
        return cursor

    async def executemany(self, query: str, params: Iterable[Sequence[Any]]) -> None:
        await self.conn.executemany(query, params)
        await self.conn.commit()

    async def fetchall(self, query: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(query, params) as cursor:
            return list(await cursor.fetchall())

    async def fetchone(self, query: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(query, params) as cursor:
            return await cursor.fetchone()

    async def fetchval(self, query: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetchone(query, params)
        return default if row is None else row[0]

    # ── guild config ─────────────────────────────────────────────────────────

    async def get_guild_config(self, guild_id: int) -> dict[str, Any]:
        """Return a guild's settings, creating the row on first access.

        Results are cached because the message handler reads the prefix and the
        leveling toggle on every single message.
        """
        cached = self._guild_cache.get(guild_id)
        if cached is not None:
            return cached

        row = await self.fetchone("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,))
        if row is None:
            await self.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
            row = await self.fetchone("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,))

        config = dict(row) if row is not None else {"guild_id": guild_id}
        self._guild_cache[guild_id] = config
        return config

    async def set_guild_setting(self, guild_id: int, key: str, value: Any) -> None:
        if key not in GUILD_SETTINGS:
            raise KeyError(f"{key!r} is not a configurable guild setting")
        await self.get_guild_config(guild_id)  # ensure the row exists
        await self.execute(
            f"UPDATE guild_config SET {key} = ? WHERE guild_id = ?",  # key is whitelisted above
            (value, guild_id),
        )
        self._guild_cache.pop(guild_id, None)

    def invalidate_guild(self, guild_id: int) -> None:
        self._guild_cache.pop(guild_id, None)

    # ── bot-wide settings ────────────────────────────────────────────────────

    async def get_bot_settings(self) -> dict[str, str | None]:
        """Every stored bot-wide setting. Missing keys mean "never set"."""
        if self._bot_cache is None:
            rows = await self.fetchall("SELECT key, value FROM bot_settings")
            self._bot_cache = {row["key"]: row["value"] for row in rows}
        return dict(self._bot_cache)

    async def set_bot_settings(self, values: dict[str, str | None]) -> None:
        """Write several bot-wide settings at once; a presence is one unit."""
        unknown = set(values) - BOT_SETTINGS
        if unknown:
            raise KeyError(f"{sorted(unknown)} are not configurable bot settings")
        if not values:
            return
        await self.executemany(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            list(values.items()),
        )
        self._bot_cache = None
