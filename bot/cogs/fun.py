"""Light-weight fun commands."""

from __future__ import annotations

import random
import re

import discord
from discord import app_commands
from discord.ext import commands

from ..utils import embeds
from ..utils.errors import FriendlyError
from ..utils.formatting import truncate

DICE_RE = re.compile(r"^(\d{0,3})d(\d{1,4})([+-]\d{1,4})?$", re.IGNORECASE)

EIGHT_BALL = [
    "It is certain.", "Without a doubt.", "You may rely on it.", "Yes, definitely.",
    "As I see it, yes.", "Most likely.", "Outlook good.", "Signs point to yes.",
    "Reply hazy, try again.", "Ask again later.", "Better not tell you now.",
    "Cannot predict now.", "Concentrate and ask again.", "Don't count on it.",
    "My reply is no.", "My sources say no.", "Outlook not so good.", "Very doubtful.",
]

POLL_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


class Fun(commands.Cog):
    """Dice, polls, and other nonsense."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(name="8ball", aliases=["eightball"])
    @app_commands.describe(question="What you want to know")
    async def eightball(self, ctx: commands.Context, *, question: str) -> None:
        """Ask the magic 8-ball."""
        embed = embeds.info(f"🎱 {random.choice(EIGHT_BALL)}", title=truncate(question, 250))
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="coinflip", aliases=["flip", "coin"])
    async def coinflip(self, ctx: commands.Context) -> None:
        """Flip a coin."""
        result = random.choice(("Heads", "Tails"))
        await ctx.send(embed=embeds.info(f"🪙 **{result}**"))

    @commands.hybrid_command(name="roll", aliases=["dice"])
    @app_commands.describe(dice="Standard dice notation, e.g. `2d6`, `d20`, `4d8+2`")
    async def roll(self, ctx: commands.Context, dice: str = "1d6") -> None:
        """Roll dice."""
        match = DICE_RE.match(dice.strip().replace(" ", ""))
        if not match:
            raise FriendlyError("Use dice notation like `2d6`, `d20`, or `4d8+2`.")

        count = int(match.group(1) or 1)
        sides = int(match.group(2))
        modifier = int(match.group(3) or 0)

        if not 1 <= count <= 100:
            raise FriendlyError("Roll between 1 and 100 dice.")
        if not 2 <= sides <= 1000:
            raise FriendlyError("Dice need between 2 and 1000 sides.")

        rolls = [random.randint(1, sides) for _ in range(count)]
        total = sum(rolls) + modifier

        detail = ", ".join(str(value) for value in rolls)
        description = f"🎲 **{total}**"
        if count > 1 or modifier:
            description += f"\n`{truncate(detail, 500)}`"
            if modifier:
                description += f" {'+' if modifier > 0 else '−'} {abs(modifier)}"
        await ctx.send(embed=embeds.info(description, title=f"Rolling {dice}"))

    @commands.hybrid_command(name="choose", aliases=["pick"])
    @app_commands.describe(options="Options separated by commas, or by `or`")
    async def choose(self, ctx: commands.Context, *, options: str) -> None:
        """Pick one of several options."""
        parts = [part.strip() for part in re.split(r",|\bor\b", options) if part.strip()]
        if len(parts) < 2:
            raise FriendlyError("Give me at least two options, separated by commas.")
        await ctx.send(
            embed=embeds.info(f"I pick **{truncate(random.choice(parts), 200)}**."),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="poll")
    @app_commands.describe(
        question="What you're asking",
        option1="First choice", option2="Second choice", option3="Third choice",
        option4="Fourth choice", option5="Fifth choice",
    )
    @commands.guild_only()
    async def poll(
        self,
        ctx: commands.Context,
        question: str,
        option1: str,
        option2: str,
        option3: str | None = None,
        option4: str | None = None,
        option5: str | None = None,
    ) -> None:
        """Run a poll with up to five options and live vote counts."""
        options = [option for option in (option1, option2, option3, option4, option5) if option]
        view = PollView(question, options, ctx.author.id)
        view.message = await ctx.send(embed=view.render(), view=view)

    @commands.hybrid_command(name="say")
    @app_commands.describe(message="What the bot should say")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def say(self, ctx: commands.Context, *, message: str) -> None:
        """Have the bot repeat something. Never pings roles or @everyone."""
        await ctx.send(
            truncate(message, 1900),
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        if ctx.interaction is None:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass


class PollView(discord.ui.View):
    """Votes live in memory: a bot restart ends the poll."""

    def __init__(self, question: str, options: list[str], author_id: int) -> None:
        super().__init__(timeout=86400)  # 24 hours
        self.question = question
        self.options = options
        self.author_id = author_id
        self.votes: dict[int, int] = {}  # user id -> option index
        self.closed = False
        self.message: discord.Message | None = None

        for index, option in enumerate(options):
            self.add_item(PollButton(index, option))

    def render(self) -> discord.Embed:
        counts = [0] * len(self.options)
        for choice in self.votes.values():
            counts[choice] += 1
        total = sum(counts) or 1

        lines = []
        for index, option in enumerate(self.options):
            share = counts[index] / total
            filled = round(share * 12)
            bar = "█" * filled + "░" * (12 - filled)
            lines.append(
                f"{POLL_EMOJI[index]} **{truncate(option, 90)}**\n"
                f"`{bar}` {counts[index]} vote{'s' if counts[index] != 1 else ''} "
                f"({share * 100:.0f}%)"
            )

        embed = embeds.info("\n\n".join(lines), title=f"📊 {truncate(self.question, 240)}")
        embed.set_footer(
            text=("Poll closed" if self.closed else "Click to vote — you can change your mind")
            + f" • {sum(counts)} total"
        )
        return embed

    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=self.render(), view=self)

    async def on_timeout(self) -> None:
        self.closed = True
        if self.message is not None:
            try:
                await self.message.edit(embed=self.render(), view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="End poll", style=discord.ButtonStyle.danger, row=1)
    async def end(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        is_author = interaction.user.id == self.author_id
        permissions = getattr(interaction.user, "guild_permissions", None)
        can_manage = bool(permissions and permissions.manage_messages)
        if not (is_author or can_manage):
            await interaction.response.send_message(
                embed=embeds.error("Only whoever started the poll can end it."), ephemeral=True
            )
            return
        self.closed = True
        await interaction.response.edit_message(embed=self.render(), view=None)
        self.stop()


class PollButton(discord.ui.Button):
    def __init__(self, index: int, option: str) -> None:
        super().__init__(
            label=truncate(option, 78), emoji=POLL_EMOJI[index], style=discord.ButtonStyle.secondary, row=0
        )
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        view: PollView = self.view
        if view.closed:
            await interaction.response.send_message(
                embed=embeds.error("That poll is closed."), ephemeral=True
            )
            return
        view.votes[interaction.user.id] = self.index
        await view.refresh(interaction)


async def setup(bot) -> None:
    await bot.add_cog(Fun(bot))
