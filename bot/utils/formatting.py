"""Small formatting and parsing helpers shared across cogs."""

from __future__ import annotations

import re
from typing import Iterable

_DURATION_UNITS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "week": 604800,
    "weeks": 604800,
}

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)", re.IGNORECASE)
_CLOCK_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")


def format_duration(seconds: float | None) -> str:
    """Seconds -> `3:07` or `1:02:07`. Live streams have no duration."""
    if seconds is None:
        return "LIVE"
    seconds = int(max(0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_long_duration(seconds: float) -> str:
    """Seconds -> `2d 3h 10m`, for uptime and similar."""
    seconds = int(max(0, seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def parse_duration(value: str) -> int | None:
    """Parse `10m`, `1h30m`, `2d`, `90`, or `3:45` into seconds.

    Returns None when nothing sensible could be parsed, so callers can show a
    usage hint instead of silently doing the wrong thing.
    """
    value = value.strip().lower()
    if not value:
        return None

    clock = _CLOCK_RE.match(value)
    if clock:
        hours, minutes, secs = clock.groups()
        return int(hours or 0) * 3600 + int(minutes) * 60 + int(secs)

    if value.isdigit():
        return int(value)

    total = 0
    matched = False
    for amount, unit in _DURATION_RE.findall(value):
        multiplier = _DURATION_UNITS.get(unit)
        if multiplier is None:
            return None
        total += int(float(amount) * multiplier)
        matched = True
    return total if matched else None


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix


def progress_bar(position: float, total: float | None, length: int = 20) -> str:
    """A `────●────` style seek bar for the now-playing embed."""
    if not total or total <= 0:
        return "🔴 LIVE"
    ratio = min(1.0, max(0.0, position / total))
    filled = int(ratio * (length - 1))
    return "─" * filled + "●" + "─" * (length - 1 - filled)


def humanize_list(items: Iterable[str], conjunction: str = "and") -> str:
    items = list(items)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} {conjunction} {items[1]}"
    return ", ".join(items[:-1]) + f", {conjunction} {items[-1]}"


def escape(text: str) -> str:
    """Neutralise markdown so user-supplied names cannot break an embed."""
    return re.sub(r"([*_~`|>\\])", r"\\\1", text)
