"""The vocabulary shared by ACTIVITY_TYPE in .env, the owner-only `status`
command, and the presence restored from the database at startup.

Keeping it in one place means a status set from Discord and one set from the
environment are built, described, and stored the same way.
"""

from __future__ import annotations

import discord

# Plain `discord.Activity` types. `streaming` and `custom` are handled
# separately in build_activity because they are different classes.
ACTIVITY_TYPES: dict[str, discord.ActivityType] = {
    "playing": discord.ActivityType.playing,
    "listening": discord.ActivityType.listening,
    "watching": discord.ActivityType.watching,
    "competing": discord.ActivityType.competing,
}

# How Discord renders each type in front of the text.
ACTIVITY_PREFIXES: dict[str, str] = {
    "playing": "Playing",
    "listening": "Listening to",
    "watching": "Watching",
    "competing": "Competing in",
    "streaming": "Streaming",
}

STATUSES: dict[str, discord.Status] = {
    "online": discord.Status.online,
    "idle": discord.Status.idle,
    "dnd": discord.Status.dnd,
    "invisible": discord.Status.invisible,
}

STATUS_LABELS: dict[str, str] = {
    "online": "🟢 Online",
    "idle": "🌙 Idle",
    "dnd": "⛔ Do Not Disturb",
    "invisible": "⚫ Invisible (offline to everyone else)",
}

# Discord truncates longer names, so reject them with a clear message instead.
MAX_ACTIVITY_NAME = 128

# Stored in place of a type to mean "deliberately no activity", which is
# different from "nothing saved, use whatever .env says".
NO_ACTIVITY = "none"


def build_activity(kind: str | None, name: str | None, url: str | None = None) -> discord.BaseActivity | None:
    """Turn a stored (kind, name, url) triple back into an activity object."""
    if not kind or kind == NO_ACTIVITY or not name:
        return None
    if kind == "streaming":
        return discord.Streaming(name=name, url=url or "")
    if kind == "custom":
        return discord.CustomActivity(name=name)
    activity_type = ACTIVITY_TYPES.get(kind)
    if activity_type is None:
        return None
    return discord.Activity(type=activity_type, name=name)


def describe(activity: discord.BaseActivity | None) -> str:
    """One line reading the way Discord shows the activity under the bot's name."""
    if activity is None:
        return "*nothing*"
    if isinstance(activity, discord.CustomActivity):
        return f"**{activity.name}**"
    if isinstance(activity, discord.Streaming):
        url = f" — <{activity.url}>" if activity.url else ""
        return f"Streaming **{activity.name}**{url}"
    prefix = ACTIVITY_PREFIXES.get(activity.type.name, activity.type.name.capitalize())
    return f"{prefix} **{activity.name}**"


def describe_status(status: discord.Status) -> str:
    return STATUS_LABELS.get(status.value, status.value)
