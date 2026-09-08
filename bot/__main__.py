"""Entrypoint: `python -m bot`."""

from __future__ import annotations

import asyncio
import ctypes.util
import logging
import sys

import discord

from .bot import MusicBot
from .config import ConfigError, load_config


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    root.handlers = [handler]

    # discord.py's gateway chatter is noisy at INFO and says nothing useful here.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)


async def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    setup_logging(config.log_level)
    log = logging.getLogger("bot")

    # discord.py loads libopus lazily when the first voice client is created,
    # so is_loaded() is False here even when opus is perfectly fine. Load it
    # eagerly instead, and only warn if that genuinely fails — a missing opus
    # is much easier to diagnose at boot than at the first /play.
    if not discord.opus.is_loaded():
        try:
            discord.opus.load_opus(ctypes.util.find_library("opus") or "libopus.so.0")
        except (OSError, TypeError):
            log.warning(
                "libopus could not be loaded, so voice will not work. "
                "Install libopus0 in the image (the Dockerfile already does)."
            )
        else:
            log.info("Loaded libopus for voice")

    bot = MusicBot(config)
    try:
        await bot.start(config.token)
    except discord.LoginFailure:
        log.error("Discord rejected the token. Check DISCORD_TOKEN in .env.")
        return 1
    except discord.PrivilegedIntentsRequired:
        log.error(
            "This bot needs the Message Content and Server Members intents. Enable both under "
            "Bot -> Privileged Gateway Intents at https://discord.com/developers/applications"
        )
        return 1
    finally:
        if not bot.is_closed():
            await bot.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0)
