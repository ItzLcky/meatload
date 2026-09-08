"""Small reusable views."""

from __future__ import annotations

import discord

from . import embeds


class Confirm(discord.ui.View):
    """A yes/no prompt locked to the person who ran the command."""

    def __init__(
        self,
        author_id: int,
        *,
        confirm_label: str = "Confirm",
        confirm_style: discord.ButtonStyle = discord.ButtonStyle.success,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.confirmed = False
        self.message: discord.Message | None = None
        self.confirm.label = confirm_label
        self.confirm.style = confirm_style

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=embeds.error("That prompt isn't yours."), ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(
                    embed=embeds.neutral("Timed out — nothing was changed."), view=None
                )
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.confirmed = True
        await interaction.response.edit_message(embed=embeds.neutral("Working…"), view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=embeds.neutral("Cancelled."), view=None)
        self.stop()
