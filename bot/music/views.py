"""Buttons attached to the now-playing message."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from ..utils import embeds
from ..utils.formatting import format_duration
from .queue import LoopMode

if TYPE_CHECKING:
    from .player import GuildPlayer


async def can_control(interaction: discord.Interaction, player: "GuildPlayer") -> bool:
    """Same rule as the DJ check, phrased for a button press.

    Anyone in the voice channel can drive the player unless a DJ role is
    configured and more than one person is listening.
    """
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False

    channel = player.channel
    if channel is None or member not in channel.members:
        await interaction.response.send_message(
            embed=embeds.error("Join the voice channel first."), ephemeral=True
        )
        return False

    if member.guild_permissions.manage_guild:
        return True

    config = await player.bot.db.get_guild_config(member.guild.id)
    dj_role_id = config.get("dj_role_id")
    if not dj_role_id or any(role.id == dj_role_id for role in member.roles):
        return True

    listeners = [m for m in channel.members if not m.bot]
    if len(listeners) <= 1:
        return True

    role = member.guild.get_role(dj_role_id)
    if role is None:
        return True
    await interaction.response.send_message(
        embed=embeds.error(f"You need the **{role.name}** role while others are listening."),
        ephemeral=True,
    )
    return False


class PlayerControls(discord.ui.View):
    """Persists for the lifetime of the track, not the bot — a restart drops it."""

    def __init__(self, player: "GuildPlayer") -> None:
        super().__init__(timeout=None)
        self.player = player
        self.message: discord.Message | None = None
        self._sync()

    def _sync(self) -> None:
        self.playpause.emoji = "▶️" if self.player.is_paused else "⏸️"
        self.loop.style = (
            discord.ButtonStyle.secondary
            if self.player.loop_mode is LoopMode.OFF
            else discord.ButtonStyle.success
        )

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=self.player.now_playing_embed(), view=self)

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.secondary, row=0)
    async def playpause(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await can_control(interaction, self.player):
            return
        if self.player.is_paused:
            self.player.resume()
        else:
            self.player.pause()
        await self._refresh(interaction)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await can_control(interaction, self.player):
            return
        skipped = self.player.skip()
        if skipped is None:
            await interaction.response.send_message(
                embed=embeds.error("Nothing to skip."), ephemeral=True
            )
            return
        # The player loop replaces this message with the next track's controls.
        await interaction.response.edit_message(view=None)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop_playback(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await can_control(interaction, self.player):
            return
        await interaction.response.edit_message(
            embed=embeds.neutral(f"⏹️ Stopped by {interaction.user.mention}."), view=None
        )
        await self.player.destroy()

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=0)
    async def loop(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await can_control(interaction, self.player):
            return
        self.player.loop_mode = self.player.loop_mode.cycled()
        await self._refresh(interaction)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=0)
    async def shuffle(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await can_control(interaction, self.player):
            return
        if len(self.player.queue) < 2:
            await interaction.response.send_message(
                embed=embeds.error("Not enough tracks queued to shuffle."), ephemeral=True
            )
            return
        self.player.queue.shuffle()
        await interaction.response.send_message(
            embed=embeds.success(f"Shuffled {len(self.player.queue)} queued tracks."), ephemeral=True
        )

    @discord.ui.button(label="Queue", emoji="📜", style=discord.ButtonStyle.secondary, row=1)
    async def show_queue(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        queue = self.player.queue
        if not len(queue):
            await interaction.response.send_message(
                embed=embeds.info("The queue is empty."), ephemeral=True
            )
            return
        lines = [
            f"`{index}.` {track.display(50)} `{format_duration(track.duration)}`"
            for index, track in enumerate(list(queue)[:10], start=1)
        ]
        if len(queue) > 10:
            lines.append(f"\n…and **{len(queue) - 10}** more.")
        await interaction.response.send_message(
            embed=embeds.info("\n".join(lines), title=f"Up next ({len(queue)})"), ephemeral=True
        )
