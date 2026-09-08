"""Button paginator for anything that can overflow one embed."""

from __future__ import annotations

import discord

from . import embeds


class Paginator(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], author_id: int, *, timeout: float = 180.0) -> None:
        super().__init__(timeout=timeout)
        self.pages = pages
        self.author_id = author_id
        self.index = 0
        self.message: discord.Message | None = None

        # A single page needs no controls at all.
        if len(pages) <= 1:
            self.clear_items()
        else:
            self._sync()

    def _sync(self) -> None:
        self.first.disabled = self.previous.disabled = self.index == 0
        self.last.disabled = self.next.disabled = self.index >= len(self.pages) - 1
        self.counter.label = f"{self.index + 1}/{len(self.pages)}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=embeds.error("Run the command yourself to page through it."),
                ephemeral=True,
            )
            return False
        return True

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = 0
        await self._show(interaction)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = max(0, self.index - 1)
        await self._show(interaction)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.primary, disabled=True)
    async def counter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        pass  # display only

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = min(len(self.pages) - 1, self.index + 1)
        await self._show(interaction)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = len(self.pages) - 1
        await self._show(interaction)


async def send_pages(ctx, pages: list[discord.Embed]) -> None:
    """Send `pages` with a paginator attached, handling the empty case."""
    if not pages:
        await ctx.send(embed=embeds.info("Nothing to show."))
        return
    view = Paginator(pages, ctx.author.id)
    view.message = await ctx.send(embed=pages[0], view=view if view.children else None)
