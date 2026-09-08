"""Import Red-DiscordBot custom commands straight into the database.

    python -m bot.tools.import_red <source> --guild-id <id>

`<source>` can be the `settings.json` itself, Red's data directory, or a Red
instance name (looked up in Red's own config). Nothing is written without
`--apply`, so the default run is a safe preview.

`/tag import` in Discord does the same job by uploading the file, and is easier
if the bot is in a container. This exists for scripted or offline use, and for
inspecting a file before letting it near the database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from .. import redimport
from ..db import Database

RED_CONFIG_LOCATIONS = (
    Path.home() / ".config" / "Red-DiscordBot" / "config.json",
    Path("/data/.config/Red-DiscordBot/config.json"),
    Path("/config/.config/Red-DiscordBot/config.json"),
)

SETTINGS_CANDIDATES = (
    "cogs/CustomCommands/settings.json",
    "CustomCommands/settings.json",
    "settings.json",
)


class SourceError(Exception):
    pass


async def _load_reserved_names() -> set[str]:
    """Every root command name and alias the bot already uses.

    Tags are dispatched from the command-not-found handler, so a tag named
    `play` would be permanently shadowed by the music command. `/tag import`
    knows this from the live bot; the CLI has to build the same list by loading
    the cogs offline, or the two import paths would disagree.
    """
    os.environ.setdefault("DISCORD_TOKEN", "offline-import")

    from ..bot import EXTENSIONS, MusicBot
    from ..config import load_config

    bot = MusicBot(load_config())
    bot._ready = asyncio.Event()  # background loops wait on this; it never fires
    reserved: set[str] = set()
    try:
        for extension in EXTENSIONS:
            await bot.load_extension(extension)
        for command in bot.commands:
            reserved.add(command.name)
            reserved.update(command.aliases)
    finally:
        for extension in reversed(list(bot.extensions)):
            try:
                await bot.unload_extension(extension)
            except Exception:  # noqa: BLE001 - teardown of a throwaway bot
                pass
    return reserved


def reserved_names() -> set[str]:
    try:
        return asyncio.run(_load_reserved_names())
    except Exception as exc:  # noqa: BLE001
        print(
            f"warning: couldn't inspect the bot's own commands ({exc}). "
            "Imported names won't be checked against built-ins.",
            file=sys.stderr,
        )
        return set()


def _from_instance_name(name: str) -> Path:
    """Resolve a Red instance name through Red's own config file."""
    for config_path in RED_CONFIG_LOCATIONS:
        if not config_path.is_file():
            continue
        try:
            instances = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if name not in instances:
            available = ", ".join(sorted(instances)) or "none"
            raise SourceError(f"No Red instance called {name!r}. Found: {available}")

        instance = instances[name]
        storage = str(instance.get("STORAGE_TYPE", "JSON")).upper()
        if storage not in {"JSON", ""}:
            raise SourceError(
                f"Instance {name!r} stores its data in {storage}, not JSON files. "
                "Point this at an exported settings.json instead, or temporarily "
                "convert the instance with `redbot-setup convert`."
            )

        data_path = Path(instance.get("DATA_PATH", "")).expanduser()
        candidate = data_path / "cogs" / "CustomCommands" / "settings.json"
        if not candidate.is_file():
            raise SourceError(
                f"Instance {name!r} has no custom commands at {candidate} "
                "(the CustomCommands cog may never have been used)."
            )
        return candidate

    raise SourceError(
        f"{name!r} isn't a file or directory, and no Red config was found to look it up in. "
        "Pass the path to CustomCommands/settings.json directly."
    )


def locate_settings(source: str) -> Path:
    path = Path(source).expanduser()
    if path.is_file():
        return path
    if path.is_dir():
        for candidate in SETTINGS_CANDIDATES:
            if (path / candidate).is_file():
                return path / candidate
        raise SourceError(
            f"{path} is a directory but has no CustomCommands/settings.json under it. "
            f"Looked for: {', '.join(SETTINGS_CANDIDATES)}"
        )
    return _from_instance_name(source)


async def apply(db_path: str, guild_id: int, report: redimport.ImportReport, overwrite: bool) -> None:
    db = Database(db_path)
    await db.connect()
    try:
        await db.migrate()
        created = updated = skipped = 0
        now = time.time()

        for command in report.commands:
            row = await db.fetchone(
                "SELECT id FROM tags WHERE guild_id = ? AND name = ?", (guild_id, command.name)
            )
            if row is not None and not overwrite:
                skipped += 1
                continue
            if row is not None:
                await db.execute(
                    "UPDATE tags SET content = ?, is_random = ?, updated_at = ? WHERE id = ?",
                    (command.content, int(command.is_random), now, row["id"]),
                )
                updated += 1
                continue

            cursor = await db.execute(
                "INSERT INTO tags (guild_id, name, content, owner_id, created_at, updated_at, is_random) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    guild_id,
                    command.name,
                    command.content,
                    command.author_id or 0,
                    command.created_at or now,
                    now,
                    int(command.is_random),
                ),
            )
            created += 1
            for alias in command.aliases:
                await db.execute(
                    "INSERT OR IGNORE INTO tag_aliases (guild_id, alias, tag_id) VALUES (?, ?, ?)",
                    (guild_id, alias, cursor.lastrowid),
                )

        print(f"\nWrote {created} new, {updated} replaced, {skipped} left alone -> {db_path}")
        if skipped:
            print("Re-run with --overwrite to replace the ones that already existed.")
    finally:
        await db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bot.tools.import_red",
        description="Import Red-DiscordBot custom commands into this bot.",
    )
    parser.add_argument(
        "source",
        help="Path to CustomCommands/settings.json, Red's data directory, or a Red instance name",
    )
    parser.add_argument(
        "--guild-id", type=int, help="Server to import into. Defaults to the source server."
    )
    parser.add_argument(
        "--source-guild-id", type=int, help="Which server to read, if the file covers several"
    )
    parser.add_argument(
        "--db",
        default=os.getenv("DATABASE_PATH", "data/bot.db"),
        help="Path to the bot's SQLite database (default: %(default)s)",
    )
    parser.add_argument("--overwrite", action="store_true", help="Replace tags that already exist")
    parser.add_argument("--apply", action="store_true", help="Actually write. Without it, preview only.")
    args = parser.parse_args(argv)

    try:
        settings_path = locate_settings(args.source)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Reading {settings_path}")
    try:
        data = redimport.load_json(settings_path.read_text(encoding="utf-8"))
        report, label = redimport.build_any_report(
            data,
            args.source_guild_id or args.guild_id,
            reserved_names=reserved_names(),
        )
    except (redimport.RedImportError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Detected: {label} export" + (f" for server {report.guild_id}" if report.guild_id else ""))
    for line in redimport.iter_summary_lines(report):
        print(line)

    if not args.apply:
        print("\nPreview only. Re-run with --apply to write these into the database.")
        return 0

    target = args.guild_id or report.guild_id
    if not target:
        print(
            "error: --guild-id is required to write, because the source didn't name a server.",
            file=sys.stderr,
        )
        return 1
    if not report.commands:
        print("Nothing to write.")
        return 0

    asyncio.run(apply(args.db, target, report, args.overwrite))
    return 0


if __name__ == "__main__":
    sys.exit(main())
