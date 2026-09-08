"""A single colour palette and embed constructors, so every cog looks the same."""

from __future__ import annotations

import discord

BLURPLE = discord.Color(0x5865F2)
GREEN = discord.Color(0x57F287)
RED = discord.Color(0xED4245)
YELLOW = discord.Color(0xFEE75C)
GREY = discord.Color(0x2B2D31)


def _embed(color: discord.Color, description: str, title: str | None) -> discord.Embed:
    return discord.Embed(color=color, title=title, description=description)


def success(description: str, title: str | None = None) -> discord.Embed:
    return _embed(GREEN, f"✅ {description}", title)


def error(description: str, title: str | None = None) -> discord.Embed:
    return _embed(RED, f"❌ {description}", title)


def warning(description: str, title: str | None = None) -> discord.Embed:
    return _embed(YELLOW, f"⚠️ {description}", title)


def info(description: str, title: str | None = None) -> discord.Embed:
    return _embed(BLURPLE, description, title)


def neutral(description: str, title: str | None = None) -> discord.Embed:
    return _embed(GREY, description, title)
