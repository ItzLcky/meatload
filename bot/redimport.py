"""Convert Red-DiscordBot custom commands into this bot's tags.

Deliberately free of Discord and database imports: everything here is pure
data-in/data-out so the tricky parts (placeholder translation, tolerant parsing
of Red's on-disk shape) can be tested directly. Both the `/tag import` command
and the `bot.tools.import_red` CLI drive this module.

Red's JSON driver writes `<data>/cogs/CustomCommands/settings.json` shaped like:

    {"414589031223512": {"GUILD": {"<guild id>": {"commands": {
        "name": {"response": "...", "author": {"id": 1}, "created_at": "..."}
    }}}}}

Rather than hard-coding that path through the tree, the extractor walks the
document looking for the recognisable shape. That way it survives Red version
differences, hand-edited files, and the much older v2 layout.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

# Red's cog identifier for CustomCommands. Only used to recognise a file, never
# required — the extractor works without it.
RED_CUSTOMCOM_IDENTIFIER = "414589031223512"

MAX_NAME_LENGTH = 32
NAME_RE = re.compile(r"^[a-z0-9_\-]{1,32}$")
_INVALID_NAME_CHARS = re.compile(r"[^a-z0-9_\-]+")
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")

# Guild snowflakes are 17-20 digits. The bound matters: it keeps the walker from
# mistaking Red's 15-digit cog identifier for a guild ID.
_SNOWFLAKE_RE = re.compile(r"^\d{17,20}$")

# How Red's placeholders map onto ours.
#
# Two of these are easy to get wrong. Red renders `{channel}` as `str(channel)`,
# which is the channel *name*, whereas ours is a mention — so it maps to
# `{channel.name}`, not `{channel}`. Likewise Red's `{author}` is `str(member)`,
# the username, not a mention.
PLACEHOLDER_MAP: dict[str, str] = {
    "author": "{user.tag}",
    "author.name": "{user.username}",
    "author.display_name": "{user.name}",
    "author.nick": "{user.name}",
    "author.mention": "{user}",
    "author.id": "{user.id}",
    "author.avatar_url": "{user.avatar}",
    "author.display_avatar": "{user.avatar}",
    "channel": "{channel.name}",
    "channel.name": "{channel.name}",
    "channel.mention": "{channel}",
    "channel.id": "{channel.id}",
    "guild": "{server}",
    "guild.name": "{server}",
    "guild.id": "{server.id}",
    "guild.member_count": "{server.members}",
    "guild.icon_url": "{server.icon}",
    "server": "{server}",
    "server.name": "{server}",
    "server.id": "{server.id}",
    "server.member_count": "{server.members}",
    "server.icon_url": "{server.icon}",
}


class RedImportError(Exception):
    """The supplied file isn't Red custom-command data we can read."""


@dataclass(slots=True)
class ImportedCommand:
    source_name: str
    name: str
    responses: list[str]
    author_id: int | None = None
    created_at: float | None = None
    unknown_placeholders: set[str] = field(default_factory=set)
    had_cooldowns: bool = False
    aliases: list[str] = field(default_factory=list)

    @property
    def renamed(self) -> bool:
        return self.name != self.source_name

    @property
    def is_random(self) -> bool:
        return len(self.responses) > 1

    @property
    def content(self) -> str:
        """What goes in the `tags.content` column.

        Multi-response commands are stored as a JSON array and flagged with
        `is_random`; single ones stay plain text.
        """
        return json.dumps(self.responses) if self.is_random else self.responses[0]

    @property
    def notes(self) -> list[str]:
        notes: list[str] = []
        if self.renamed:
            notes.append(f"renamed from `{self.source_name}`")
        if self.is_random:
            notes.append(f"{len(self.responses)} random responses")
        if self.unknown_placeholders:
            placeholders = ", ".join(f"`{item}`" for item in sorted(self.unknown_placeholders))
            notes.append(f"unsupported placeholder(s) left as-is: {placeholders}")
        if self.had_cooldowns:
            notes.append("cooldown not carried over")
        return notes


@dataclass(slots=True)
class ImportReport:
    guild_id: int | None
    available_guild_ids: list[int]
    commands: list[ImportedCommand]
    skipped: list[tuple[str, str]]

    @property
    def needs_attention(self) -> list[ImportedCommand]:
        return [command for command in self.commands if command.notes]


# ── locating and reading Red's data ──────────────────────────────────────────


def looks_like_red_data(data: Any) -> bool:
    """Whether this document contains anything resembling Red custom commands."""
    return bool(extract_guild_commands(data))


def load_json(raw: str | bytes) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RedImportError(f"That file isn't valid JSON ({exc.msg}, line {exc.lineno}).") from exc


def _is_snowflake(key: str) -> bool:
    return bool(_SNOWFLAKE_RE.match(key))


def _is_command_entry(value: Any) -> bool:
    return isinstance(value, dict) and "response" in value


def extract_guild_commands(data: Any) -> dict[int | None, dict[str, Any]]:
    """Pull `{guild_id: {name: entry}}` out of a Red settings document.

    Walks the whole tree instead of following a fixed path, tracking the nearest
    ancestor key that looks like a guild ID. Handles the modern Red 3 layout,
    the flat `{guild: {name: "response"}}` used by Red 2, and files that have
    been trimmed down by hand.
    """
    found: dict[int | None, dict[str, Any]] = {}

    def record(guild_id: int | None, name: str, entry: Any) -> None:
        found.setdefault(guild_id, {})[name] = entry

    def walk(node: Any, guild_id: int | None) -> None:
        if not isinstance(node, dict):
            if isinstance(node, list):
                for item in node:
                    walk(item, guild_id)
            return

        for key, value in node.items():
            if not isinstance(key, str):
                continue

            current = int(key) if _is_snowflake(key) else guild_id

            if key == "commands" and isinstance(value, dict):
                entries = {
                    name: entry for name, entry in value.items() if _is_command_entry(entry)
                }
                if entries:
                    for name, entry in entries.items():
                        record(current, name, entry)
                    continue

            # Red 2: {"<guild id>": {"name": "response text"}}
            if _is_snowflake(key) and isinstance(value, dict) and value:
                if all(isinstance(item, str) for item in value.values()):
                    for name, response in value.items():
                        record(current, name, {"response": response})
                    continue

            walk(value, current)

    walk(data, None)
    return found


# ── translation ──────────────────────────────────────────────────────────────


def translate_response(text: str) -> tuple[str, set[str]]:
    """Rewrite Red placeholders as ours.

    Returns the translated text plus any placeholders that had no equivalent.
    Unknown ones are left exactly as they were rather than being dropped, so
    nothing silently disappears from a response.
    """
    unknown: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if not inner:
            return match.group(0)
        # Positional arguments mean the same thing in both bots.
        if inner.isdigit():
            return "{" + inner + "}"
        mapped = PLACEHOLDER_MAP.get(inner.lower())
        if mapped is not None:
            return mapped
        unknown.add(match.group(0))
        return match.group(0)

    return _PLACEHOLDER_RE.sub(replace, text), unknown


def sanitise_name(name: str) -> str | None:
    """Coerce a Red command name into a legal tag name, or None if impossible."""
    cleaned = _INVALID_NAME_CHARS.sub("-", name.strip().lower()).strip("-")
    cleaned = cleaned[:MAX_NAME_LENGTH].strip("-")
    return cleaned or None


def _coerce_responses(raw: Any) -> list[str]:
    """Red stores either one response or a list of them (picked at random)."""
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str) and item.strip()]
    return []


def _parse_author_id(entry: dict[str, Any]) -> int | None:
    author = entry.get("author")
    if isinstance(author, dict):
        author = author.get("id")
    try:
        return int(author)
    except (TypeError, ValueError):
        return None


def _parse_timestamp(entry: dict[str, Any]) -> float | None:
    raw = entry.get("created_at")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def build_report(
    data: Any,
    guild_id: int | None = None,
    *,
    reserved_names: set[str] | None = None,
) -> ImportReport:
    """Turn a parsed Red settings document into an actionable import plan.

    `reserved_names` are this bot's built-in command names: a Red command called
    `play` would be permanently shadowed, so it is skipped and reported rather
    than imported into a tag that can never fire.
    """
    reserved = reserved_names or set()
    by_guild = extract_guild_commands(data)
    if not by_guild:
        raise RedImportError(
            "No Red custom commands were found in that file. Make sure it's the "
            "`CustomCommands/settings.json` from Red's data directory."
        )

    available = sorted(gid for gid in by_guild if gid is not None)

    if guild_id is not None:
        if guild_id in by_guild:
            entries = by_guild[guild_id]
        elif set(by_guild) == {None}:
            # A hand-trimmed file with no guild IDs in it. The caller naming a
            # server is the only signal available, so take their word for it.
            entries = by_guild[None]
        else:
            # An explicit server ID that isn't in the file is a mistake worth
            # reporting, even when there is exactly one other candidate —
            # silently importing the wrong server's commands would be worse.
            raise RedImportError(
                f"That file has no commands for server `{guild_id}`. "
                f"It contains: {', '.join(f'`{gid}`' for gid in available) or 'no server IDs'}."
            )
    elif len(by_guild) == 1:
        only_key = next(iter(by_guild))
        entries = by_guild[only_key]
        guild_id = only_key
    else:
        raise RedImportError(
            "That file covers several servers. Re-run with the source server ID, one of: "
            + ", ".join(f"`{gid}`" for gid in available)
        )

    commands: list[ImportedCommand] = []
    skipped: list[tuple[str, str]] = []
    seen: dict[str, str] = {}

    for source_name, entry in sorted(entries.items()):
        if not isinstance(entry, dict):
            skipped.append((source_name, "unrecognised entry"))
            continue

        responses = _coerce_responses(entry.get("response"))
        if not responses:
            skipped.append((source_name, "empty response"))
            continue

        name = sanitise_name(source_name)
        if name is None:
            skipped.append((source_name, "name has no usable characters"))
            continue
        if name in reserved:
            skipped.append((source_name, f"`{name}` is a built-in command"))
            continue
        if name in seen:
            skipped.append((source_name, f"collides with `{seen[name]}` after renaming"))
            continue
        seen[name] = source_name

        translated: list[str] = []
        unknown: set[str] = set()
        for response in responses:
            text, missing = translate_response(response)
            translated.append(text)
            unknown |= missing

        commands.append(
            ImportedCommand(
                source_name=source_name,
                name=name,
                responses=translated,
                author_id=_parse_author_id(entry),
                created_at=_parse_timestamp(entry),
                unknown_placeholders=unknown,
                had_cooldowns=bool(entry.get("cooldowns")),
            )
        )

    return ImportReport(
        guild_id=guild_id,
        available_guild_ids=available,
        commands=commands,
        skipped=skipped,
    )


def iter_summary_lines(report: ImportReport) -> Iterator[str]:
    """Human-readable summary, shared by the Discord preview and the CLI."""
    yield f"{len(report.commands)} command(s) ready to import"
    if report.skipped:
        yield f"{len(report.skipped)} skipped"
    for command in report.needs_attention:
        yield f"  {command.name}: {'; '.join(command.notes)}"
    for name, reason in report.skipped:
        yield f"  skipped {name}: {reason}"


# ── this bot's own export format ─────────────────────────────────────────────

NATIVE_FORMAT = "discordbot-tags/1"


def build_native_report(data: Any, *, reserved_names: set[str] | None = None) -> ImportReport:
    """Read a file produced by `/tag export`, so backups can be restored."""
    reserved = reserved_names or set()
    tags = data.get("tags")
    if not isinstance(tags, list):
        raise RedImportError("That backup has no `tags` list.")

    commands: list[ImportedCommand] = []
    skipped: list[tuple[str, str]] = []
    for item in tags:
        if not isinstance(item, dict):
            continue
        source_name = str(item.get("name", "")).strip()
        raw = item.get("responses")
        responses = _coerce_responses(raw if raw is not None else item.get("content"))
        if not source_name or not responses:
            skipped.append((source_name or "<unnamed>", "empty name or response"))
            continue
        name = sanitise_name(source_name)
        if name is None:
            skipped.append((source_name, "name has no usable characters"))
            continue
        if name in reserved:
            skipped.append((source_name, f"`{name}` is a built-in command"))
            continue
        commands.append(
            ImportedCommand(
                source_name=source_name,
                name=name,
                responses=responses,
                author_id=_parse_author_id(item) or item.get("owner_id"),
                created_at=_parse_timestamp(item),
                aliases=[
                    alias
                    for alias in (sanitise_name(str(a)) for a in item.get("aliases") or [])
                    if alias
                ],
            )
        )

    guild_id = data.get("guild_id")
    return ImportReport(
        guild_id=int(guild_id) if isinstance(guild_id, (int, str)) and str(guild_id).isdigit() else None,
        available_guild_ids=[],
        commands=commands,
        skipped=skipped,
    )


def build_any_report(
    data: Any, guild_id: int | None = None, *, reserved_names: set[str] | None = None
) -> tuple[ImportReport, str]:
    """Detect the file's flavour and build a report. Returns (report, label)."""
    if isinstance(data, dict) and data.get("format") == NATIVE_FORMAT:
        return build_native_report(data, reserved_names=reserved_names), "backup"
    return build_report(data, guild_id, reserved_names=reserved_names), "Red"
