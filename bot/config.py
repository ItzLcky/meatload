"""Environment-backed configuration.

Everything the bot needs to boot comes from environment variables so the
container is configured entirely through `.env` / compose. Values are read once
at startup into a frozen dataclass.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import discord
from dotenv import load_dotenv

from .utils.presence import ACTIVITY_TYPES, build_activity

_TRUE = {"1", "true", "yes", "y", "on"}
_SPLIT = re.compile(r"[,\s]+")


def _str(key: str, default: str = "") -> str:
    value = os.getenv(key)
    return default if value is None else value.strip()


def _bool(key: str, default: bool = False) -> bool:
    value = os.getenv(key)
    return default if value is None else value.strip().lower() in _TRUE


def _int(key: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = _str(key)
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _id_set(key: str) -> set[int]:
    return {int(part) for part in _SPLIT.split(_str(key)) if part.isdigit()}


def _activity_type() -> str:
    """ACTIVITY_TYPE, falling back to `listening` if it is misspelled."""
    value = _str("ACTIVITY_TYPE", "listening").lower()
    return value if value in ACTIVITY_TYPES else "listening"


class ConfigError(RuntimeError):
    """Raised when the bot cannot start with the configuration it was given."""


@dataclass(frozen=True, slots=True)
class Config:
    token: str
    default_prefix: str
    owner_ids: set[int]
    dev_guild_ids: set[int]
    log_level: str
    database_path: str
    activity_name: str
    activity_type: str

    music_idle_timeout: int
    music_alone_timeout: int
    music_default_volume: int
    music_max_queue: int
    music_max_playlist: int
    ytdlp_cookies_file: str
    ytdlp_extractor_args: str

    spotify_client_id: str
    spotify_client_secret: str

    heartbeat_path: str = field(default="/app/data/heartbeat")

    @property
    def spotify_enabled(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)

    @property
    def activity(self) -> discord.BaseActivity | None:
        return build_activity(self.activity_type, self.activity_name)


def load_config() -> Config:
    load_dotenv()

    token = _str("DISCORD_TOKEN")
    if not token:
        raise ConfigError(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and paste the token "
            "from https://discord.com/developers/applications -> your app -> Bot."
        )

    database_path = _str("DATABASE_PATH", "/app/data/bot.db")
    cookies = _str("YTDLP_COOKIES_FILE")
    if cookies and not os.path.exists(cookies):
        # Not fatal: a missing cookies file just means we fall back to
        # anonymous extraction, which works most of the time.
        cookies = ""

    return Config(
        token=token,
        default_prefix=_str("DEFAULT_PREFIX", "!") or "!",
        owner_ids=_id_set("OWNER_IDS"),
        dev_guild_ids=_id_set("DEV_GUILD_IDS"),
        log_level=_str("LOG_LEVEL", "INFO").upper() or "INFO",
        database_path=database_path,
        activity_name=_str("ACTIVITY_NAME"),
        activity_type=_activity_type(),
        music_idle_timeout=_int("MUSIC_IDLE_TIMEOUT", 300, minimum=30),
        music_alone_timeout=_int("MUSIC_ALONE_TIMEOUT", 60, minimum=5),
        music_default_volume=_int("MUSIC_DEFAULT_VOLUME", 50, minimum=0, maximum=200),
        music_max_queue=_int("MUSIC_MAX_QUEUE", 500, minimum=1),
        music_max_playlist=_int("MUSIC_MAX_PLAYLIST", 200, minimum=1),
        ytdlp_cookies_file=cookies,
        ytdlp_extractor_args=_str("YTDLP_EXTRACTOR_ARGS"),
        spotify_client_id=_str("SPOTIFY_CLIENT_ID"),
        spotify_client_secret=_str("SPOTIFY_CLIENT_SECRET"),
        heartbeat_path=os.path.join(os.path.dirname(database_path) or ".", "heartbeat"),
    )
