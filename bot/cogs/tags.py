"""Custom commands.

A tag is a named snippet the server can create at runtime. They are invoked
either through `/tag show <name>` or, more naturally, straight off the prefix:
`!welcome`. The prefix form is wired up from the command-not-found handler, so
tags never shadow a real command.

For servers coming from Red, the whole group answers to `cc` as well, and the
subcommand names match Red's: `cc list`, `cc add`, `cc del`, `cc edit`,
`cc show`. Aliases only apply to the prefix form — Discord has no such thing
for slash commands, so those stay under `/tag`.
"""

from __future__ import annotations

import io
import json
import random
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from .. import redimport
from ..utils import checks, embeds
from ..utils.errors import FriendlyError
from ..utils.formatting import truncate
from ..utils.paginator import send_pages
from ..utils.views import Confirm

NAME_RE = re.compile(r"^[a-z0-9_\-]{1,32}$")

# Tags are user-authored, so never let one ping a role or @everyone.
TAG_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)

MAX_CONTENT = 1900

# A Red custom-command export is a few KB; anything near this is the wrong file.
MAX_IMPORT_BYTES = 8 * 1024 * 1024

# Red-style positional arguments: {0} is the first word typed after the name.
POSITIONAL_RE = re.compile(r"\{(\d{1,2})\}")

# Red spells creation as `[p]cc create simple <name> <text>` / `[p]cc create
# random <name>`. We have no such sub-sub-commands, so the keyword is unwrapped
# instead of being taken for the tag's name — see `split_red_create_syntax`.
# That only happens under the Red spelling of the group, which keeps `!tag
# create simple ...` free to make a tag genuinely called `simple`.
RED_CREATE_KEYWORDS = ("simple", "random")
RED_GROUP_ALIASES = ("cc", "customcom")

VARIABLE_HELP = (
    "**Who ran it** `{user}` mention • `{user.name}` display name • "
    "`{user.username}` • `{user.tag}` • `{user.id}` • `{user.avatar}`\n"
    "**Where** `{server}` • `{server.id}` • `{server.members}` • `{server.icon}` • "
    "`{channel}` mention • `{channel.name}` • `{channel.id}`\n"
    "**Input** `{args}` everything typed after the name • `{0}` `{1}` … individual words\n"
    "**Meta** `{count}` times used"
)


def pick_all_responses(row) -> list[str]:
    """Every response a tag can give, for export."""
    if not row["is_random"]:
        return [row["content"]]
    try:
        options = json.loads(row["content"])
    except (TypeError, ValueError):
        return [row["content"]]
    return [item for item in options if isinstance(item, str)]


def pick_response(row) -> str:
    """A tag holds either one response or a JSON array to choose between."""
    content = row["content"]
    if not row["is_random"]:
        return content
    try:
        options = json.loads(content)
    except (TypeError, ValueError):
        return content
    return random.choice(options) if options else ""


def render(content: str, ctx: commands.Context, uses: int, args: str) -> str:
    """Substitute the variables tags support.

    Deliberately not a real template engine: `str.format` on user content would
    happily expose object internals through `{0.__class__}`-style access.
    """
    guild = ctx.guild
    author = ctx.author
    channel = ctx.channel
    icon = getattr(guild, "icon", None)
    replacements = {
        "{user}": author.mention,
        "{user.name}": author.display_name,
        "{user.username}": author.name,
        "{user.tag}": str(author),
        "{user.id}": str(author.id),
        "{user.avatar}": author.display_avatar.url,
        "{server}": guild.name if guild else "this server",
        "{guild}": guild.name if guild else "this server",
        "{server.id}": str(guild.id) if guild else "",
        "{server.members}": str(guild.member_count or 0) if guild else "0",
        "{server.icon}": icon.url if icon else "",
        "{channel}": getattr(channel, "mention", "this channel"),
        "{channel.name}": getattr(channel, "name", "this channel"),
        "{channel.id}": str(getattr(channel, "id", "")),
        "{count}": str(uses),
        "{args}": args,
    }
    for key, value in replacements.items():
        content = content.replace(key, value)

    words = args.split()
    return POSITIONAL_RE.sub(
        lambda match: words[int(match.group(1))] if int(match.group(1)) < len(words) else "",
        content,
    )


def split_red_create_syntax(
    name: str, content: str, usage: str = "!cc "
) -> tuple[str | None, str, str]:
    """Unwrap Red's `cc create simple|random <name> <text>` into ours.

    Someone migrating from Red types `!cc add simple hello Hi!`, which would
    otherwise create a tag literally called `simple` whose response starts with
    the name they actually wanted. Returns `(keyword, name, content)` with the
    keyword consumed, or `(None, name, content)` when that first word was meant
    as the tag's own name after all.

    Raises when the keyword is followed by a bare name and nothing else
    (`!cc add random greet`, Red's interactive form): both readings are
    plausible there, so guessing either way would be worse than asking.
    """
    keyword = name.strip().lower()
    if keyword not in RED_CREATE_KEYWORDS:
        return None, name, content

    candidate, _, rest = content.strip().partition(" ")
    if not NAME_RE.match(candidate.lower()):
        # Not a usable name, so `simple`/`random` really was the tag name.
        return None, name, content
    if not rest.strip():
        raise FriendlyError(
            f"`{keyword} {candidate}` is Red's syntax, which needs the response on the "
            f"same line here:\n"
            f"• `{usage}add {candidate} <text>` — one fixed response\n"
            f"• `{usage}random {candidate} Hi!|Hello!|Hey` — a random one each time"
        )
    return keyword, candidate, rest.strip()


class Tags(commands.Cog):
    """Server-defined custom commands."""

    def __init__(self, bot) -> None:
        self.bot = bot

    async def _lookup(self, guild_id: int, name: str):
        """Resolve a name or alias to a tag row."""
        name = name.strip().lower()
        row = await self.bot.db.fetchone(
            "SELECT * FROM tags WHERE guild_id = ? AND name = ?", (guild_id, name)
        )
        if row is not None:
            return row
        return await self.bot.db.fetchone(
            "SELECT t.* FROM tags t JOIN tag_aliases a ON a.tag_id = t.id "
            "WHERE a.guild_id = ? AND a.alias = ?",
            (guild_id, name),
        )

    async def _send_tag(self, ctx: commands.Context, row, args: str) -> None:
        await self.bot.db.execute("UPDATE tags SET uses = uses + 1 WHERE id = ?", (row["id"],))
        content = render(pick_response(row), ctx, row["uses"] + 1, args)
        await ctx.send(content, allowed_mentions=TAG_MENTIONS)

    async def try_invoke_prefix_tag(self, ctx: commands.Context) -> bool:
        """Called when a prefix command wasn't found: maybe it's a tag.

        Returns True if a tag was sent, so the caller can stay quiet.
        """
        if ctx.guild is None or not ctx.invoked_with:
            return False
        row = await self._lookup(ctx.guild.id, ctx.invoked_with)
        if row is None:
            return False

        # Everything after the tag name becomes {args}.
        message = ctx.message.content
        _, _, rest = message.partition(ctx.invoked_with)
        await self._send_tag(ctx, row, rest.strip())
        return True

    def _invoked_group(self, ctx: commands.Context) -> str:
        """Which spelling of the group was typed: `tag`, `t`, `cc`, `customcom`."""
        return ctx.invoked_parents[0].lower() if ctx.invoked_parents else "tag"

    def _group_usage(self, ctx: commands.Context) -> str:
        """`!cc ` or `!tag `, whichever the caller actually typed.

        Examples in error messages should echo the spelling the server uses, so
        a Red refugee working in `cc` isn't told to go and run `tag` instead.
        """
        return f"{ctx.clean_prefix}{self._invoked_group(ctx)} "

    def _validate_name(self, name: str) -> str:
        name = name.strip().lower()
        if not NAME_RE.match(name):
            raise FriendlyError(
                "Tag names must be 1-32 characters of letters, numbers, `-` or `_`, with no spaces."
            )
        if self.bot.get_command(name) is not None:
            raise FriendlyError(f"**{name}** is already a built-in command.")
        return name

    # ── commands ─────────────────────────────────────────────────────────────

    @commands.hybrid_group(
        name="tag", aliases=["t", "cc", "customcom"], fallback="show", invoke_without_command=True
    )
    @app_commands.describe(name="Which tag to show", args="Optional text, available as {args}")
    @commands.guild_only()
    async def tag(self, ctx: commands.Context, name: str, *, args: str = "") -> None:
        """Show a custom command."""
        row = await self._lookup(ctx.guild.id, name)
        if row is None and name.strip().lower() == "show" and args:
            # `!cc show hello` is how Red spells it. `show` is this group's
            # app-command fallback, so it can't also be a real subcommand —
            # unwrap it here, but only once a tag genuinely called `show` has
            # been ruled out.
            name, _, rest = args.strip().partition(" ")
            args = rest.strip()
            row = await self._lookup(ctx.guild.id, name)
        if row is None:
            suggestions = await self.bot.db.fetchall(
                "SELECT name FROM tags WHERE guild_id = ? AND name LIKE ? ORDER BY uses DESC LIMIT 5",
                (ctx.guild.id, f"%{name.strip().lower()}%"),
            )
            hint = ""
            if suggestions:
                hint = "\nDid you mean: " + ", ".join(f"`{row['name']}`" for row in suggestions)
            raise FriendlyError(f"No tag called **{truncate(name, 40)}**.{hint}")
        await self._send_tag(ctx, row, args)

    @tag.command(name="create", aliases=["add", "new"])
    @app_commands.describe(name="Name to invoke it by", content="What the bot should reply with")
    @checks.can_manage_tags()
    async def tag_create(self, ctx: commands.Context, name: str, *, content: str) -> None:
        """Create a custom command."""
        if self._invoked_group(ctx) in RED_GROUP_ALIASES:
            keyword, name, content = split_red_create_syntax(name, content, self._group_usage(ctx))
            if keyword == "random":
                await self._create_random(ctx, name, content)
                return

        name = self._validate_name(name)
        if len(content) > MAX_CONTENT:
            raise FriendlyError(f"Tag content is limited to {MAX_CONTENT} characters.")

        if await self._lookup(ctx.guild.id, name) is not None:
            raise FriendlyError(
                f"**{name}** already exists. Use `{self._group_usage(ctx)}edit` to change it."
            )

        now = time.time()
        await self.bot.db.execute(
            "INSERT INTO tags (guild_id, name, content, owner_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ctx.guild.id, name, content, ctx.author.id, now, now),
        )
        await ctx.send(
            embed=embeds.success(
                f"Created **{name}**. Run it with `{ctx.clean_prefix}{name}` or `/tag show {name}`."
            )
        )

    @tag.command(name="edit")
    @app_commands.describe(name="Tag to edit", content="The new content")
    @checks.can_manage_tags()
    async def tag_edit(self, ctx: commands.Context, name: str, *, content: str) -> None:
        """Change what a custom command says."""
        row = await self._lookup(ctx.guild.id, name)
        if row is None:
            raise FriendlyError(f"No tag called **{truncate(name, 40)}**.")
        if len(content) > MAX_CONTENT:
            raise FriendlyError(f"Tag content is limited to {MAX_CONTENT} characters.")

        await self.bot.db.execute(
            "UPDATE tags SET content = ?, updated_at = ? WHERE id = ?", (content, time.time(), row["id"])
        )
        await ctx.send(embed=embeds.success(f"Updated **{row['name']}**."))

    @tag.command(name="delete", aliases=["del", "remove", "rm"])
    @app_commands.describe(name="Tag to delete")
    @checks.can_manage_tags()
    async def tag_delete(self, ctx: commands.Context, *, name: str) -> None:
        """Delete a custom command."""
        row = await self._lookup(ctx.guild.id, name)
        if row is None:
            raise FriendlyError(f"No tag called **{truncate(name, 40)}**.")
        await self.bot.db.execute("DELETE FROM tags WHERE id = ?", (row["id"],))
        await ctx.send(embed=embeds.success(f"Deleted **{row['name']}** and its aliases."))

    @tag.command(name="alias")
    @app_commands.describe(alias="New name to also respond to", name="Existing tag it points at")
    @checks.can_manage_tags()
    async def tag_alias(self, ctx: commands.Context, alias: str, *, name: str) -> None:
        """Add another name for an existing tag."""
        alias = self._validate_name(alias)
        row = await self._lookup(ctx.guild.id, name)
        if row is None:
            raise FriendlyError(f"No tag called **{truncate(name, 40)}**.")
        if await self._lookup(ctx.guild.id, alias) is not None:
            raise FriendlyError(f"**{alias}** is already taken.")

        await self.bot.db.execute(
            "INSERT INTO tag_aliases (guild_id, alias, tag_id) VALUES (?, ?, ?)",
            (ctx.guild.id, alias, row["id"]),
        )
        await ctx.send(embed=embeds.success(f"**{alias}** now points at **{row['name']}**."))

    @tag.command(name="raw")
    @app_commands.describe(name="Tag to show the source of")
    async def tag_raw(self, ctx: commands.Context, *, name: str) -> None:
        """Show a tag's source, so you can copy it before editing."""
        row = await self._lookup(ctx.guild.id, name)
        if row is None:
            raise FriendlyError(f"No tag called **{truncate(name, 40)}**.")
        escaped = discord.utils.escape_markdown(row["content"])
        await ctx.send(
            embed=embeds.neutral(f"```\n{truncate(escaped, 1900)}\n```", title=f"Source of {row['name']}")
        )

    @tag.command(name="info")
    @app_commands.describe(name="Tag to inspect")
    async def tag_info(self, ctx: commands.Context, *, name: str) -> None:
        """Show who made a tag and how often it's used."""
        row = await self._lookup(ctx.guild.id, name)
        if row is None:
            raise FriendlyError(f"No tag called **{truncate(name, 40)}**.")

        aliases = await self.bot.db.fetchall(
            "SELECT alias FROM tag_aliases WHERE tag_id = ? ORDER BY alias", (row["id"],)
        )
        embed = embeds.info("", title=row["name"])
        embed.add_field(name="Created by", value=f"<@{row['owner_id']}>", inline=True)
        embed.add_field(name="Uses", value=str(row["uses"]), inline=True)
        embed.add_field(name="Created", value=f"<t:{int(row['created_at'])}:R>", inline=True)
        if aliases:
            embed.add_field(
                name="Aliases",
                value=", ".join(f"`{alias['alias']}`" for alias in aliases),
                inline=False,
            )
        await ctx.send(embed=embed)

    @tag.command(name="all", aliases=["list", "ls"])
    async def tag_all(self, ctx: commands.Context) -> None:
        """List every custom command on this server."""
        rows = await self.bot.db.fetchall(
            "SELECT name, uses FROM tags WHERE guild_id = ? ORDER BY uses DESC, name", (ctx.guild.id,)
        )
        if not rows:
            await ctx.send(
                embed=embeds.info(
                    f"No custom commands yet. Make one with "
                    f"`{self._group_usage(ctx)}add <name> <content>`.\n\nVariables: {VARIABLE_HELP}"
                )
            )
            return

        pages: list[discord.Embed] = []
        per_page = 20
        for start in range(0, len(rows), per_page):
            chunk = rows[start : start + per_page]
            lines = [f"`{row['name']}` — {row['uses']} use{'s' if row['uses'] != 1 else ''}" for row in chunk]
            embed = embeds.info("\n".join(lines), title=f"Custom commands ({len(rows)})")
            embed.set_footer(text=f"Run one with {ctx.clean_prefix}<name>")
            pages.append(embed)
        await send_pages(ctx, pages)

    @tag.command(name="search")
    @app_commands.describe(query="Text to look for in names and content")
    async def tag_search(self, ctx: commands.Context, *, query: str) -> None:
        """Search custom commands."""
        pattern = f"%{query.strip().lower()}%"
        rows = await self.bot.db.fetchall(
            "SELECT name FROM tags WHERE guild_id = ? AND (name LIKE ? OR lower(content) LIKE ?) "
            "ORDER BY uses DESC LIMIT 25",
            (ctx.guild.id, pattern, pattern),
        )
        if not rows:
            raise FriendlyError(f"Nothing matched **{truncate(query, 40)}**.")
        await ctx.send(
            embed=embeds.info(
                ", ".join(f"`{row['name']}`" for row in rows), title=f"{len(rows)} match(es)"
            )
        )

    @tag.command(name="random")
    @app_commands.describe(
        name="Name to invoke it by",
        responses="Responses separated by `|`, one picked at random each time",
    )
    @checks.can_manage_tags()
    async def tag_random(self, ctx: commands.Context, name: str, *, responses: str) -> None:
        """Create a custom command that answers with a random response."""
        await self._create_random(ctx, name, responses)

    async def _create_random(self, ctx: commands.Context, name: str, responses: str) -> None:
        """Shared by `tag random` and Red's `cc add random <name> <a>|<b>`."""
        name = self._validate_name(name)
        options = [part.strip() for part in responses.split("|") if part.strip()]
        if len(options) < 2:
            raise FriendlyError(
                "Give at least two responses separated by `|`, for example:\n"
                f"`{self._group_usage(ctx)}random greet Hi!|Hello!|Hey there`"
            )

        content = json.dumps(options)
        if len(content) > MAX_CONTENT:
            raise FriendlyError(f"Those responses total more than {MAX_CONTENT} characters.")
        if await self._lookup(ctx.guild.id, name) is not None:
            raise FriendlyError(f"**{name}** already exists.")

        now = time.time()
        await self.bot.db.execute(
            "INSERT INTO tags (guild_id, name, content, owner_id, created_at, updated_at, is_random) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (ctx.guild.id, name, content, ctx.author.id, now, now),
        )
        await ctx.send(
            embed=embeds.success(f"Created **{name}** with **{len(options)}** random responses.")
        )

    # ── migrating from Red ───────────────────────────────────────────────────

    def _reserved_names(self) -> set[str]:
        """Root command names and their aliases — a tag could never shadow one."""
        reserved: set[str] = set()
        for command in self.bot.commands:
            reserved.add(command.name)
            reserved.update(command.aliases)
        return reserved

    async def _classify(
        self, guild_id: int, imported: list, overwrite: bool
    ) -> tuple[list, list, list[tuple[str, str]]]:
        """Split the plan into new tags, replacements, and things we won't touch."""
        to_create, to_update, blocked = [], [], []
        for command in imported:
            alias = await self.bot.db.fetchone(
                "SELECT alias FROM tag_aliases WHERE guild_id = ? AND alias = ?",
                (guild_id, command.name),
            )
            if alias is not None:
                blocked.append((command.name, "an alias here already uses that name"))
                continue

            row = await self.bot.db.fetchone(
                "SELECT id FROM tags WHERE guild_id = ? AND name = ?", (guild_id, command.name)
            )
            if row is None:
                to_create.append(command)
            elif overwrite:
                to_update.append((command, row["id"]))
            else:
                blocked.append((command.name, "already exists here (use `overwrite: True` to replace)"))
        return to_create, to_update, blocked

    @tag.command(name="import")
    @app_commands.describe(
        file="Red's CustomCommands/settings.json, or a file from /tag export",
        source_server="Source server ID — only needed if the file covers several servers",
        overwrite="Replace tags here that have the same name",
    )
    @commands.has_permissions(manage_guild=True)
    async def tag_import(
        self,
        ctx: commands.Context,
        file: discord.Attachment,
        source_server: str | None = None,
        overwrite: bool = False,
    ) -> None:
        """Bulk-import custom commands from Red-DiscordBot."""
        await ctx.defer()

        if file.size > MAX_IMPORT_BYTES:
            raise FriendlyError(
                f"That file is {file.size / 1_000_000:.1f} MB. The limit is "
                f"{MAX_IMPORT_BYTES // 1_000_000} MB — a custom-command export should be far smaller, "
                "so check you attached `CustomCommands/settings.json` and not Red's whole data folder."
            )

        try:
            data = redimport.load_json(await file.read())
            report, label = redimport.build_any_report(
                data,
                int(source_server) if source_server and source_server.strip().isdigit() else None,
                reserved_names=self._reserved_names(),
            )
        except redimport.RedImportError as exc:
            raise FriendlyError(str(exc)) from exc
        except discord.HTTPException as exc:
            raise FriendlyError(f"Couldn't download that attachment: {exc}") from exc

        if not report.commands:
            raise FriendlyError(
                "That file parsed fine but had no importable commands.\n"
                + "\n".join(f"• `{name}` — {reason}" for name, reason in report.skipped[:10])
            )

        to_create, to_update, blocked = await self._classify(ctx.guild.id, report.commands, overwrite)
        planned = to_create + [command for command, _ in to_update]
        if not planned:
            raise FriendlyError(
                "Nothing left to import — every command already exists here. "
                "Re-run with `overwrite: True` to replace them."
            )

        view = Confirm(ctx.author.id, confirm_label=f"Import {len(planned)}")
        view.message = await ctx.send(
            embed=self._import_preview(report, label, to_create, to_update, blocked),
            view=view,
        )
        await view.wait()
        if not view.confirmed:
            return

        created, updated, aliased = 0, 0, 0
        now = time.time()
        for command in to_create:
            cursor = await self.bot.db.execute(
                "INSERT INTO tags (guild_id, name, content, owner_id, created_at, updated_at, is_random) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ctx.guild.id,
                    command.name,
                    command.content,
                    command.author_id or ctx.author.id,
                    command.created_at or now,
                    now,
                    int(command.is_random),
                ),
            )
            created += 1
            for alias in command.aliases:
                # Aliases are best-effort: one already in use must not abort the import.
                cursor2 = await self.bot.db.execute(
                    "INSERT OR IGNORE INTO tag_aliases (guild_id, alias, tag_id) VALUES (?, ?, ?)",
                    (ctx.guild.id, alias, cursor.lastrowid),
                )
                aliased += cursor2.rowcount

        for command, tag_id in to_update:
            await self.bot.db.execute(
                "UPDATE tags SET content = ?, is_random = ?, updated_at = ? WHERE id = ?",
                (command.content, int(command.is_random), now, tag_id),
            )
            updated += 1

        summary = f"Imported **{created}** new custom command(s)."
        if updated:
            summary += f"\nReplaced **{updated}** existing one(s)."
        if aliased:
            summary += f"\nRestored **{aliased}** alias(es)."
        if blocked:
            summary += f"\nLeft **{len(blocked)}** untouched."
        summary += f"\n\nTry one now: `{ctx.clean_prefix}{planned[0].name}`"
        await ctx.send(embed=embeds.success(summary, title="Import complete"))

    def _import_preview(
        self, report, label: str, to_create: list, to_update: list, blocked: list
    ) -> discord.Embed:
        source = f"{label} export"
        if report.guild_id:
            source += f" (server `{report.guild_id}`)"

        description = (
            f"**Source:** {source}\n"
            f"**New tags:** {len(to_create)}\n"
            f"**Replacing:** {len(to_update)}\n"
            f"**Not touched:** {len(blocked) + len(report.skipped)}"
        )
        embed = embeds.warning(description, title="Ready to import")

        names = [command.name for command in to_create] + [c.name for c, _ in to_update]
        if names:
            shown = " ".join(f"`{name}`" for name in names[:40])
            if len(names) > 40:
                shown += f" …and {len(names) - 40} more"
            embed.add_field(name="Commands", value=truncate(shown, 1020), inline=False)

        attention = report.needs_attention
        if attention:
            lines = [f"• `{command.name}` — {'; '.join(command.notes)}" for command in attention[:8]]
            if len(attention) > 8:
                lines.append(f"…and {len(attention) - 8} more")
            embed.add_field(name="Worth checking", value=truncate("\n".join(lines), 1020), inline=False)

        ignored = blocked + report.skipped
        if ignored:
            lines = [f"• `{name}` — {reason}" for name, reason in ignored[:8]]
            if len(ignored) > 8:
                lines.append(f"…and {len(ignored) - 8} more")
            embed.add_field(name="Skipped", value=truncate("\n".join(lines), 1020), inline=False)

        embed.set_footer(text="Nothing has been written yet.")
        return embed

    @tag.command(name="export")
    @commands.has_permissions(manage_guild=True)
    async def tag_export(self, ctx: commands.Context) -> None:
        """Download every custom command here as a JSON backup."""
        rows = await self.bot.db.fetchall(
            "SELECT * FROM tags WHERE guild_id = ? ORDER BY name", (ctx.guild.id,)
        )
        if not rows:
            raise FriendlyError("There are no custom commands here to export.")

        alias_rows = await self.bot.db.fetchall(
            "SELECT alias, tag_id FROM tag_aliases WHERE guild_id = ?", (ctx.guild.id,)
        )
        aliases: dict[int, list[str]] = {}
        for row in alias_rows:
            aliases.setdefault(row["tag_id"], []).append(row["alias"])

        payload = {
            "format": redimport.NATIVE_FORMAT,
            "guild_id": str(ctx.guild.id),
            "exported_at": time.time(),
            "tags": [
                {
                    "name": row["name"],
                    "responses": pick_all_responses(row),
                    "owner_id": row["owner_id"],
                    "uses": row["uses"],
                    "created_at": row["created_at"],
                    "aliases": aliases.get(row["id"], []),
                }
                for row in rows
            ],
        }
        buffer = io.BytesIO(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))
        await ctx.send(
            embed=embeds.success(f"Exported **{len(rows)}** custom command(s)."),
            file=discord.File(buffer, filename=f"tags-{ctx.guild.id}.json"),
        )

    @tag.command(name="variables", aliases=["vars"])
    async def tag_variables(self, ctx: commands.Context) -> None:
        """List the variables you can use inside a tag."""
        await ctx.send(embed=embeds.info(VARIABLE_HELP, title="Tag variables"))


async def setup(bot) -> None:
    await bot.add_cog(Tags(bot))
