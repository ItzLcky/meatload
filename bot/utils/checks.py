"""Reusable command checks.

Each check raises a specific error so `on_command_error` can explain exactly
what was missing instead of falling back to "check failed".
"""

from __future__ import annotations

import discord
from discord.ext import commands


class NotDJ(commands.CheckFailure):
    def __init__(self, role: discord.Role) -> None:
        self.role = role
        super().__init__(
            f"You need the **{role.name}** role to do that while other people are listening."
        )


class NotTagManager(commands.CheckFailure):
    def __init__(self) -> None:
        super().__init__(
            "You need the **Manage Messages** permission or the configured tag-manager role to do that."
        )


def _has_role(member: discord.Member, role_id: int | None) -> bool:
    return bool(role_id) and any(role.id == role_id for role in member.roles)


def is_dj():
    """Gate disruptive music commands (skip, stop, clear, volume, seek).

    Deliberately permissive: with no DJ role configured, or when the caller is
    the only human in the voice channel, anyone can drive the player. The role
    only starts mattering once other people are actually listening.
    """

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()

        member = ctx.author
        assert isinstance(member, discord.Member)

        if member.guild_permissions.manage_guild:
            return True

        config = await ctx.bot.db.get_guild_config(ctx.guild.id)
        dj_role_id = config.get("dj_role_id")
        if not dj_role_id:
            return True
        if _has_role(member, dj_role_id):
            return True

        voice = ctx.guild.voice_client
        if voice is not None and voice.channel is not None:
            listeners = [m for m in voice.channel.members if not m.bot]
            if len(listeners) <= 1 and member in listeners:
                return True

        role = ctx.guild.get_role(dj_role_id)
        if role is None:
            return True  # role was deleted; don't lock everyone out
        raise NotDJ(role)

    return commands.check(predicate)


def can_manage_tags():
    """Who may create/edit/delete custom commands."""

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()

        member = ctx.author
        assert isinstance(member, discord.Member)

        if member.guild_permissions.manage_messages:
            return True

        config = await ctx.bot.db.get_guild_config(ctx.guild.id)
        if _has_role(member, config.get("tag_manager_role_id")):
            return True

        raise NotTagManager()

    return commands.check(predicate)


def can_act_on(actor: discord.Member, target: discord.Member) -> str | None:
    """Return a refusal reason if `actor` may not moderate `target`, else None.

    Discord enforces role hierarchy server-side, but checking here produces a
    readable message instead of a 403 traceback.
    """
    if actor.id == target.id:
        return "You cannot use that on yourself."
    if target.id == actor.guild.owner_id:
        return "You cannot use that on the server owner."
    if actor.id != actor.guild.owner_id and actor.top_role <= target.top_role:
        return "That member has a role equal to or higher than yours."
    me = actor.guild.me
    if me.top_role <= target.top_role:
        return "That member's highest role is above mine, so I can't act on them."
    return None
