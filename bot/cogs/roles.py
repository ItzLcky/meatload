"""Self-assign role panels, driven by buttons.

Buttons are `DynamicItem`s rather than a stored View per panel: their handler
is registered once at startup and matches on the custom_id, so panels keep
working across restarts without re-registering every message.
"""

from __future__ import annotations

import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from ..utils import embeds
from ..utils.errors import FriendlyError
from ..utils.formatting import truncate

MAX_BUTTONS = 25  # 5 rows x 5 buttons


class RoleButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"rolepanel:(?P<panel_id>\d+):(?P<role_id>\d+)",
):
    def __init__(self, panel_id: int, role_id: int, label: str, emoji: str | None = None) -> None:
        self.panel_id = panel_id
        self.role_id = role_id
        super().__init__(
            discord.ui.Button(
                label=truncate(label, 80),
                emoji=emoji or None,
                style=discord.ButtonStyle.secondary,
                custom_id=f"rolepanel:{panel_id}:{role_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> "RoleButton":
        return cls(
            int(match["panel_id"]),
            int(match["role_id"]),
            item.label or "Role",
            str(item.emoji) if item.emoji else None,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member):
            return

        role = guild.get_role(self.role_id)
        if role is None:
            await interaction.response.send_message(
                embed=embeds.error("That role no longer exists."), ephemeral=True
            )
            return
        if role >= guild.me.top_role:
            await interaction.response.send_message(
                embed=embeds.error(f"**{role.name}** is above my highest role, so I can't assign it."),
                ephemeral=True,
            )
            return

        db = interaction.client.db

        if role in member.roles:
            await member.remove_roles(role, reason="Role panel")
            await interaction.response.send_message(
                embed=embeds.success(f"Removed **{role.name}**."), ephemeral=True
            )
            return

        panel = await db.fetchone("SELECT max_roles FROM role_panels WHERE id = ?", (self.panel_id,))
        max_roles = panel["max_roles"] if panel else 0

        if max_roles:
            items = await db.fetchall(
                "SELECT role_id FROM role_panel_items WHERE panel_id = ?", (self.panel_id,)
            )
            panel_role_ids = {item["role_id"] for item in items}
            held = [r for r in member.roles if r.id in panel_role_ids]

            if len(held) >= max_roles:
                if max_roles == 1:
                    # A one-choice panel behaves like a radio button: swap.
                    await member.remove_roles(*held, reason="Role panel (single choice)")
                else:
                    await interaction.response.send_message(
                        embed=embeds.error(
                            f"You can only pick **{max_roles}** roles from this panel. "
                            "Remove one first."
                        ),
                        ephemeral=True,
                    )
                    return

        await member.add_roles(role, reason="Role panel")
        await interaction.response.send_message(
            embed=embeds.success(f"Added **{role.name}**."), ephemeral=True
        )


class Roles(commands.Cog):
    """Let members pick their own roles from a panel of buttons."""

    def __init__(self, bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(RoleButton)

    # ── rendering ────────────────────────────────────────────────────────────

    async def _build(self, panel_id: int) -> tuple[discord.Embed, discord.ui.View]:
        panel = await self.bot.db.fetchone("SELECT * FROM role_panels WHERE id = ?", (panel_id,))
        if panel is None:
            raise FriendlyError(f"No panel with ID {panel_id}.")

        items = await self.bot.db.fetchall(
            "SELECT * FROM role_panel_items WHERE panel_id = ? ORDER BY position, id", (panel_id,)
        )

        description = panel["description"] or ""
        if items:
            description += "\n\n" + "\n".join(
                f"{item['emoji'] + ' ' if item['emoji'] else ''}<@&{item['role_id']}>" for item in items
            )
        else:
            description += "\n\n*No roles yet — add some with `/rolepanel add`.*"

        embed = embeds.info(description.strip(), title=panel["title"])
        if panel["max_roles"] == 1:
            embed.set_footer(text="Pick one • Panel ID " + str(panel_id))
        elif panel["max_roles"]:
            embed.set_footer(text=f"Pick up to {panel['max_roles']} • Panel ID {panel_id}")
        else:
            embed.set_footer(text=f"Click to toggle • Panel ID {panel_id}")

        view = discord.ui.View(timeout=None)
        for item in items[:MAX_BUTTONS]:
            view.add_item(RoleButton(panel_id, item["role_id"], item["label"], item["emoji"]))
        return embed, view

    async def _refresh(self, ctx: commands.Context, panel_id: int) -> None:
        """Re-render a panel's posted message after it changes."""
        panel = await self.bot.db.fetchone("SELECT * FROM role_panels WHERE id = ?", (panel_id,))
        if panel is None or not panel["message_id"]:
            return
        channel = ctx.guild.get_channel(panel["channel_id"])
        if not isinstance(channel, discord.abc.Messageable):
            return
        try:
            message = await channel.fetch_message(panel["message_id"])
        except discord.HTTPException:
            return
        embed, view = await self._build(panel_id)
        try:
            await message.edit(embed=embed, view=view)
        except discord.HTTPException:
            pass

    async def _owned_panel(self, guild_id: int, panel_id: int):
        panel = await self.bot.db.fetchone(
            "SELECT * FROM role_panels WHERE id = ? AND guild_id = ?", (panel_id, guild_id)
        )
        if panel is None:
            raise FriendlyError(f"No panel with ID {panel_id} on this server. See `/rolepanel list`.")
        return panel

    # ── commands ─────────────────────────────────────────────────────────────

    @commands.hybrid_group(name="rolepanel", aliases=["rp"], fallback="list", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def rolepanel(self, ctx: commands.Context) -> None:
        """Self-assign role panels."""
        rows = await self.bot.db.fetchall(
            "SELECT p.id, p.title, p.channel_id, COUNT(i.id) AS roles "
            "FROM role_panels p LEFT JOIN role_panel_items i ON i.panel_id = p.id "
            "WHERE p.guild_id = ? GROUP BY p.id ORDER BY p.id",
            (ctx.guild.id,),
        )
        if not rows:
            await ctx.send(
                embed=embeds.info(f"No panels yet. Create one with `{ctx.clean_prefix}rolepanel create`.")
            )
            return
        lines = [
            f"`#{row['id']}` **{row['title']}** — {row['roles']} role(s) in <#{row['channel_id']}>"
            for row in rows
        ]
        await ctx.send(embed=embeds.info("\n".join(lines), title="Role panels"))

    @rolepanel.command(name="create")
    @app_commands.describe(
        title="Heading shown on the panel",
        description="Optional text under the heading",
        max_roles="0 = unlimited, 1 = pick one, N = pick up to N",
        channel="Where to post it. Defaults to here.",
    )
    @commands.bot_has_permissions(manage_roles=True)
    async def rolepanel_create(
        self,
        ctx: commands.Context,
        title: str,
        description: str | None = None,
        max_roles: commands.Range[int, 0, 25] = 0,
        channel: discord.TextChannel | None = None,
    ) -> None:
        """Create an empty role panel and post it."""
        channel = channel or ctx.channel
        if not channel.permissions_for(ctx.guild.me).send_messages:
            raise FriendlyError(f"I can't send messages in {channel.mention}.")

        cursor = await self.bot.db.execute(
            "INSERT INTO role_panels (guild_id, channel_id, title, description, max_roles, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ctx.guild.id, channel.id, truncate(title, 250), description, max_roles, time.time()),
        )
        panel_id = cursor.lastrowid

        embed, view = await self._build(panel_id)
        message = await channel.send(embed=embed, view=view)
        await self.bot.db.execute(
            "UPDATE role_panels SET message_id = ? WHERE id = ?", (message.id, panel_id)
        )
        await ctx.send(
            embed=embeds.success(
                f"Created panel `#{panel_id}` in {channel.mention}.\n"
                f"Add roles with `{ctx.clean_prefix}rolepanel add {panel_id} @role`."
            ),
            ephemeral=True,
        )

    @rolepanel.command(name="add")
    @app_commands.describe(
        panel_id="Panel to add to, from /rolepanel list",
        role="Role members can give themselves",
        label="Button text. Defaults to the role name.",
        emoji="Optional emoji on the button",
    )
    @commands.bot_has_permissions(manage_roles=True)
    async def rolepanel_add(
        self,
        ctx: commands.Context,
        panel_id: int,
        role: discord.Role,
        label: str | None = None,
        emoji: str | None = None,
    ) -> None:
        """Add a role button to a panel."""
        await self._owned_panel(ctx.guild.id, panel_id)

        if role >= ctx.guild.me.top_role:
            raise FriendlyError(
                f"**{role.name}** is above my highest role, so I couldn't assign it. "
                "Move my role higher in Server Settings → Roles."
            )
        if role.is_default() or role.managed:
            raise FriendlyError("That role can't be self-assigned.")
        if role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
            raise FriendlyError("You can't hand out a role at or above your own highest role.")

        existing = await self.bot.db.fetchone(
            "SELECT id FROM role_panel_items WHERE panel_id = ? AND role_id = ?", (panel_id, role.id)
        )
        if existing is not None:
            raise FriendlyError(f"**{role.name}** is already on that panel.")

        count = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM role_panel_items WHERE panel_id = ?", (panel_id,), default=0
        )
        if count >= MAX_BUTTONS:
            raise FriendlyError(f"A panel can hold at most {MAX_BUTTONS} roles.")

        await self.bot.db.execute(
            "INSERT INTO role_panel_items (panel_id, role_id, label, emoji, position) VALUES (?, ?, ?, ?, ?)",
            (panel_id, role.id, label or role.name, emoji, count),
        )
        await self._refresh(ctx, panel_id)
        await ctx.send(embed=embeds.success(f"Added **{role.name}** to panel `#{panel_id}`."), ephemeral=True)

    @rolepanel.command(name="remove")
    @app_commands.describe(panel_id="Panel to edit", role="Role to take off the panel")
    async def rolepanel_remove(self, ctx: commands.Context, panel_id: int, role: discord.Role) -> None:
        """Remove a role button from a panel."""
        await self._owned_panel(ctx.guild.id, panel_id)
        cursor = await self.bot.db.execute(
            "DELETE FROM role_panel_items WHERE panel_id = ? AND role_id = ?", (panel_id, role.id)
        )
        if cursor.rowcount == 0:
            raise FriendlyError(f"**{role.name}** isn't on that panel.")
        await self._refresh(ctx, panel_id)
        await ctx.send(embed=embeds.success(f"Removed **{role.name}**."), ephemeral=True)

    @rolepanel.command(name="delete")
    @app_commands.describe(panel_id="Panel to delete")
    async def rolepanel_delete(self, ctx: commands.Context, panel_id: int) -> None:
        """Delete a panel and its message."""
        panel = await self._owned_panel(ctx.guild.id, panel_id)
        if panel["message_id"]:
            channel = ctx.guild.get_channel(panel["channel_id"])
            if isinstance(channel, discord.abc.Messageable):
                try:
                    message = await channel.fetch_message(panel["message_id"])
                    await message.delete()
                except discord.HTTPException:
                    pass
        await self.bot.db.execute("DELETE FROM role_panels WHERE id = ?", (panel_id,))
        await ctx.send(embed=embeds.success(f"Deleted panel `#{panel_id}`."), ephemeral=True)

    @rolepanel.command(name="repost")
    @app_commands.describe(panel_id="Panel to repost", channel="Where to post it")
    async def rolepanel_repost(
        self, ctx: commands.Context, panel_id: int, channel: discord.TextChannel | None = None
    ) -> None:
        """Post a fresh copy of a panel, e.g. after its message was deleted."""
        panel = await self._owned_panel(ctx.guild.id, panel_id)
        channel = channel or ctx.guild.get_channel(panel["channel_id"]) or ctx.channel
        embed, view = await self._build(panel_id)
        message = await channel.send(embed=embed, view=view)
        await self.bot.db.execute(
            "UPDATE role_panels SET message_id = ?, channel_id = ? WHERE id = ?",
            (message.id, channel.id, panel_id),
        )
        await ctx.send(embed=embeds.success(f"Reposted panel `#{panel_id}`."), ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Roles(bot))
