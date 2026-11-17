"""SQLite access layer.

One aiosqlite connection is used for the whole bot. aiosqlite runs every
statement on a single dedicated thread and serialises them internally, so a
shared connection is safe here and avoids pool bookkeeping. WAL mode keeps
reads from blocking behind the write of, say, an XP update.

Encryption at rest
------------------
The database is opened through SQLCipher (the `sqlcipher3` module, a drop-in
for `sqlite3`), so when a key is given every page of the main file *and* the
WAL is AES-256 encrypted on disk. A copied `bot.db`, a stray backup or a leaked
volume snapshot is unreadable without DATABASE_KEY. With no key SQLCipher
behaves exactly like plain SQLite, which is what the test suite uses.

A plaintext database found at startup while a key is configured is encrypted
in place once (see `_encrypt_plaintext`), so existing installs upgrade by
setting the key and restarting.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

import aiosqlite
import sqlcipher3

log = logging.getLogger(__name__)

# Re-exported so callers catch the exceptions the driver actually raises. They
# are *not* the stdlib sqlite3 classes, so `aiosqlite.IntegrityError` would
# silently fail to match.
Error = sqlcipher3.Error
IntegrityError = sqlcipher3.IntegrityError
DatabaseError = sqlcipher3.DatabaseError
Row = sqlcipher3.Row

# Every unencrypted SQLite file starts with this. An encrypted one starts with
# a random salt instead, which is how the two are told apart before opening.
_PLAINTEXT_HEADER = b"SQLite format 3\x00"

# The database file and its WAL/shared-memory siblings.
_SIDECARS = ("", "-wal", "-shm", "-journal")


class DatabaseKeyError(RuntimeError):
    """The database cannot be opened with the key (or lack of key) given."""


def _quote(key: str) -> str:
    """Quote a passphrase for `PRAGMA key`, which cannot take bound parameters."""
    return "'" + key.replace("'", "''") + "'"


def _is_plaintext(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(len(_PLAINTEXT_HEADER)) == _PLAINTEXT_HEADER
    except FileNotFoundError:
        return False


def _restrict_permissions(path: str) -> None:
    """Make the database and its sidecar files readable by their owner only."""
    for suffix in _SIDECARS:
        try:
            os.chmod(path + suffix, 0o600)
        except FileNotFoundError:
            pass
        except OSError as exc:  # e.g. a filesystem that ignores modes
            log.debug("Could not chmod %s%s: %s", path, suffix, exc)


def _open(path: str, key: str | None) -> sqlcipher3.Connection:
    """Open and unlock a connection, failing loudly on a wrong or missing key.

    Runs on aiosqlite's worker thread, which is the thread that will own the
    connection from then on.
    """
    conn = sqlcipher3.connect(path, check_same_thread=False)
    try:
        if key:
            conn.execute(f"PRAGMA key = {_quote(key)}")
        # SQLCipher only checks the key on first real read, so force one now
        # rather than letting the first command fail with a confusing error.
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlcipher3.DatabaseError as exc:
        conn.close()
        if key:
            raise DatabaseKeyError(
                f"Could not open {path}: DATABASE_KEY is wrong, or the file is corrupt."
            ) from exc
        raise DatabaseKeyError(
            f"{path} is encrypted, but no DATABASE_KEY was given to open it."
        ) from exc
    return conn


def _encrypt_plaintext(path: str, key: str) -> None:
    """Rewrite an unencrypted database as an encrypted one, in place.

    The copy is made with `sqlcipher_export` into a temporary file beside the
    original, checked to open with the key, then swapped over it atomically. If
    anything fails before the swap, the original is left untouched.
    """
    log.warning("Database at %s is not encrypted; encrypting it now.", path)
    tmp = path + ".encrypting"
    for suffix in _SIDECARS:
        try:
            os.remove(tmp + suffix)
        except FileNotFoundError:
            pass

    plain = sqlcipher3.connect(path)
    try:
        # Fold the WAL back into the main file so the export sees everything.
        plain.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        user_version = plain.execute("PRAGMA user_version").fetchone()[0]
        plain.execute(f"ATTACH DATABASE ? AS encrypted KEY {_quote(key)}", (tmp,))
        plain.execute("SELECT sqlcipher_export('encrypted')")
        plain.execute(f"PRAGMA encrypted.user_version = {int(user_version)}")
        plain.execute("DETACH DATABASE encrypted")
    finally:
        plain.close()

    _restrict_permissions(tmp)
    _open(tmp, key).close()  # proves the new file opens before we commit to it

    os.replace(tmp, path)
    for suffix in ("-wal", "-shm", "-journal"):
        for base in (path, tmp):
            try:
                os.remove(base + suffix)
            except FileNotFoundError:
                pass
    log.warning("Database encrypted. Keep DATABASE_KEY safe: without it the data is gone.")

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
        "economy_enabled",
        "cc_start_balance",
        "cc_chat_min",
        "cc_chat_max",
        "cc_chat_cooldown",
        "cc_daily_amount",
        "cc_daily_streak_bonus",
        "cc_work_min",
        "cc_work_max",
        "cc_work_cooldown",
        "culshi_enabled",
        "culshi_channel_id",
        "culshi_steward_role_id",
        "culshi_min_subsidy",
        "culshi_max_open",
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
    def __init__(self, path: str, key: str | None = None) -> None:
        self.path = path
        self._key = key or None
        self._conn: aiosqlite.Connection | None = None
        self._guild_cache: dict[int, dict[str, Any]] = {}
        self._bot_cache: dict[str, str | None] | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() has not been awaited yet")
        return self._conn

    @property
    def encrypted(self) -> bool:
        return self._key is not None

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if self._key and _is_plaintext(self.path):
            await asyncio.to_thread(_encrypt_plaintext, self.path, self._key)

        # aiosqlite.connect() hard-codes the stdlib sqlite3 module, so build the
        # Connection around our own opener instead. It runs on aiosqlite's
        # worker thread when awaited.
        path, key = self.path, self._key
        conn = aiosqlite.Connection(lambda: _open(path, key), iter_chunk_size=64)
        self._conn = await conn
        self._conn.row_factory = Row
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA synchronous = NORMAL")
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA busy_timeout = 5000")
        # Overwrite deleted rows with zeros instead of leaving them in free
        # pages, so a deleted tag or reminder is actually gone.
        await self._conn.execute("PRAGMA secure_delete = ON")
        await self._conn.commit()
        _restrict_permissions(self.path)
        log.info(
            "Connected to %s database at %s",
            "encrypted" if self.encrypted else "UNENCRYPTED",
            self.path,
        )

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

    async def fetchall(self, query: str, params: Sequence[Any] = ()) -> list[Row]:
        async with self.conn.execute(query, params) as cursor:
            return list(await cursor.fetchall())

    async def fetchone(self, query: str, params: Sequence[Any] = ()) -> Row | None:
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
