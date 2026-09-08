"""Music commands."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Literal

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..music.player import GuildPlayer
from ..music.queue import LoopMode
from ..music.source import AudioResolver, ExtractionError, Track
from ..utils import checks, embeds
from ..utils.errors import FriendlyError
from ..utils.formatting import format_duration, parse_duration, truncate
from ..utils.paginator import send_pages

log = logging.getLogger(__name__)

QUEUE_PAGE_SIZE = 10


class Music(commands.Cog):
    """Play audio from YouTube, SoundCloud, Bandcamp, direct links, and more."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}
        self.session = aiohttp.ClientSession()
        self.resolver = AudioResolver(bot.config, self.session)
        self.idle_reaper.start()

    async def cog_unload(self) -> None:
        self.idle_reaper.cancel()
        for player in list(self.players.values()):
            await player.destroy()
        await self.session.close()

    # ── player lifecycle ─────────────────────────────────────────────────────

    def get_player(self, guild_id: int) -> GuildPlayer | None:
        return self.players.get(guild_id)

    async def ensure_player(self, ctx: commands.Context, *, connect: bool = True) -> GuildPlayer:
        """Return this guild's player, connecting to the caller's channel if needed."""
        if ctx.guild is None:
            raise commands.NoPrivateMessage()

        player = self.players.get(ctx.guild.id)
        if player is not None:
            # Follow the conversation: later updates go where the last command was run.
            player.text_channel = ctx.channel
            if player.voice is not None and player.voice.is_connected():
                return player
            # Stale player from a dropped connection; rebuild it.
            await player.destroy()
            player = None

        if not connect:
            raise FriendlyError("I'm not playing anything right now.")

        voice_state = ctx.author.voice
        if voice_state is None or voice_state.channel is None:
            raise FriendlyError("Join a voice channel first.")

        channel = voice_state.channel
        permissions = channel.permissions_for(ctx.guild.me)
        if not permissions.connect:
            raise FriendlyError(f"I don't have permission to join {channel.mention}.")
        if not permissions.speak:
            raise FriendlyError(f"I don't have permission to speak in {channel.mention}.")
        if channel.user_limit and len(channel.members) >= channel.user_limit and not permissions.move_members:
            raise FriendlyError(f"{channel.mention} is full.")

        try:
            await channel.connect(self_deaf=True, timeout=20.0, reconnect=True)
        except asyncio.TimeoutError as exc:
            raise FriendlyError("Timed out connecting to voice. Try again in a moment.") from exc
        except discord.ClientException as exc:
            raise FriendlyError(f"Couldn't connect to voice: {exc}") from exc

        config = await self.bot.db.get_guild_config(ctx.guild.id)
        volume = config.get("music_volume") or self.bot.config.music_default_volume

        player = GuildPlayer(self, ctx.guild, ctx.channel, volume=volume)
        self.players[ctx.guild.id] = player
        player.start()
        return player

    def require_player(self, ctx: commands.Context) -> GuildPlayer:
        player = self.players.get(ctx.guild.id) if ctx.guild else None
        if player is None:
            raise FriendlyError("I'm not playing anything right now.")
        return player

    # ── automatic disconnects ────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        player = self.players.get(member.guild.id)
        if player is None:
            return

        # The bot itself was disconnected or moved out by someone with permissions.
        if member.id == self.bot.user.id and after.channel is None:
            await player.destroy()
            return

        channel = player.channel
        if channel is None:
            return
        listeners = [m for m in channel.members if not m.bot]
        player.alone_since = time.monotonic() if not listeners else None

    @tasks.loop(seconds=15)
    async def idle_reaper(self) -> None:
        """Leave shortly after the last human does, so the bot never lurks alone."""
        timeout = self.bot.config.music_alone_timeout
        now = time.monotonic()
        for player in list(self.players.values()):
            channel = player.channel
            if channel is None:
                await player.destroy()
                continue
            listeners = [m for m in channel.members if not m.bot]
            if listeners:
                player.alone_since = None
                continue
            if player.alone_since is None:
                player.alone_since = now
            elif now - player.alone_since >= timeout:
                await player.notify(embeds.neutral("Everyone left, so I did too. 👋"))
                await player.destroy()

    @idle_reaper.before_loop
    async def _before_reaper(self) -> None:
        await self.bot.wait_until_ready()

    # ── queueing ─────────────────────────────────────────────────────────────

    async def _enqueue(self, ctx: commands.Context, query: str, *, front: bool = False) -> None:
        await ctx.defer()
        player = await self.ensure_player(ctx)

        try:
            resolution = await self.resolver.resolve_query(query, ctx.author.id)
        except ExtractionError as exc:
            raise FriendlyError(str(exc)) from exc

        if not resolution.tracks:
            raise FriendlyError("Nothing was found for that.")

        free = player.queue.free_slots
        if free <= 0:
            raise FriendlyError(f"The queue is full ({player.queue.maxsize} tracks).")

        was_idle = player.current is None and not len(player.queue)

        if front and len(resolution.tracks) == 1:
            player.queue.put_front(resolution.tracks[0])
            added = 1
        else:
            added = player.queue.extend(resolution.tracks)

        skipped = len(resolution.tracks) - added

        if resolution.playlist_title:
            description = (
                f"Queued **{added}** track{'s' if added != 1 else ''} from "
                f"**{truncate(resolution.playlist_title, 80)}**."
            )
            if skipped:
                description += f"\n{skipped} were dropped because the queue is full."
            embed = embeds.success(description, title=f"{resolution.source_name} playlist")
        else:
            track = resolution.tracks[0]
            position = "next up" if front else f"position **{len(player.queue)}**"
            description = f"{track.display(70)}"
            if not was_idle:
                description += f"\n{position} • `{format_duration(track.duration)}`"
            embed = embeds.success(description, title="Added to queue")
            if track.thumbnail:
                embed.set_thumbnail(url=track.thumbnail)

        # The now-playing message announces the first track, so don't double up.
        if was_idle and not resolution.playlist_title:
            await ctx.send(embed=embed, delete_after=15)
        else:
            await ctx.send(embed=embed)

    @commands.hybrid_command(name="play", aliases=["p"])
    @app_commands.describe(query="Song name, or a YouTube / SoundCloud / Bandcamp / Spotify link")
    @commands.guild_only()
    async def play(self, ctx: commands.Context, *, query: str) -> None:
        """Play a track, or add it to the queue."""
        await self._enqueue(ctx, query)

    @commands.hybrid_command(name="playnext", aliases=["pn", "playtop"])
    @app_commands.describe(query="Song name or link to put at the front of the queue")
    @commands.guild_only()
    @checks.is_dj()
    async def playnext(self, ctx: commands.Context, *, query: str) -> None:
        """Add a track to the front of the queue."""
        await self._enqueue(ctx, query, front=True)

    @commands.hybrid_command(name="search")
    @app_commands.describe(query="What to search YouTube for")
    @commands.guild_only()
    async def search(self, ctx: commands.Context, *, query: str) -> None:
        """Search YouTube and pick from the top results."""
        await ctx.defer()
        try:
            data = await self.resolver._extract(f"ytsearch5:{query}", noplaylist=True, extract_flat="in_playlist")
        except ExtractionError as exc:
            raise FriendlyError(str(exc)) from exc

        entries = [entry for entry in (data.get("entries") or []) if entry][:5]
        if not entries:
            raise FriendlyError("No results.")

        tracks = [self.resolver._track_from(entry, ctx.author.id) for entry in entries]
        lines = [
            f"`{index}.` {track.display(60)} `{format_duration(track.duration)}`"
            for index, track in enumerate(tracks, start=1)
        ]
        view = SearchView(self, ctx, tracks)
        view.message = await ctx.send(
            embed=embeds.info("\n".join(lines), title=f"Results for “{truncate(query, 60)}”"),
            view=view,
        )

    # ── transport ────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="skip", aliases=["s", "next"])
    @commands.guild_only()
    @checks.is_dj()
    async def skip(self, ctx: commands.Context) -> None:
        """Skip the current track."""
        player = self.require_player(ctx)
        skipped = player.skip()
        if skipped is None:
            raise FriendlyError("Nothing is playing.")
        await ctx.send(embed=embeds.success(f"Skipped **{truncate(skipped.title, 80)}**."))

    @commands.hybrid_command(name="back", aliases=["previous", "prev"])
    @commands.guild_only()
    @checks.is_dj()
    async def back(self, ctx: commands.Context) -> None:
        """Go back to the previous track."""
        player = self.require_player(ctx)
        track = player.play_previous()
        if track is None:
            raise FriendlyError("Nothing has played yet.")
        await ctx.send(embed=embeds.success(f"Going back to **{truncate(track.title, 80)}**."))

    @commands.hybrid_command(name="pause")
    @commands.guild_only()
    @checks.is_dj()
    async def pause(self, ctx: commands.Context) -> None:
        """Pause playback."""
        player = self.require_player(ctx)
        if not player.pause():
            raise FriendlyError("Nothing is playing.")
        await player.refresh_now_playing()
        await ctx.send(embed=embeds.success("Paused."))

    @commands.hybrid_command(name="resume", aliases=["unpause"])
    @commands.guild_only()
    @checks.is_dj()
    async def resume(self, ctx: commands.Context) -> None:
        """Resume playback."""
        player = self.require_player(ctx)
        if not player.resume():
            raise FriendlyError("Playback isn't paused.")
        await player.refresh_now_playing()
        await ctx.send(embed=embeds.success("Resumed."))

    @commands.hybrid_command(name="stop", aliases=["leave", "disconnect", "dc"])
    @commands.guild_only()
    @checks.is_dj()
    async def stop(self, ctx: commands.Context) -> None:
        """Stop playback, clear the queue, and leave the voice channel."""
        player = self.require_player(ctx)
        await player.destroy()
        await ctx.send(embed=embeds.success("Stopped and disconnected."))

    @commands.hybrid_command(name="join", aliases=["summon", "connect"])
    @commands.guild_only()
    async def join(self, ctx: commands.Context) -> None:
        """Bring the bot into your voice channel."""
        await ctx.defer()
        player = await self.ensure_player(ctx)
        await ctx.send(embed=embeds.success(f"Connected to {player.channel.mention}."))

    @commands.hybrid_command(name="seek")
    @app_commands.describe(position="Where to jump to: `1:30`, `90`, or `2m30s`")
    @commands.guild_only()
    @checks.is_dj()
    async def seek(self, ctx: commands.Context, *, position: str) -> None:
        """Jump to a position in the current track."""
        player = self.require_player(ctx)
        if player.current is None:
            raise FriendlyError("Nothing is playing.")

        seconds = parse_duration(position)
        if seconds is None:
            raise FriendlyError("I couldn't read that position. Try `1:30`, `90`, or `2m30s`.")
        if player.current.is_live:
            raise FriendlyError("Live streams can't be seeked.")
        if player.current.duration and seconds >= player.current.duration:
            raise FriendlyError(
                f"That's past the end of the track (`{format_duration(player.current.duration)}`)."
            )

        player.seek(seconds)
        await ctx.send(embed=embeds.success(f"Jumped to `{format_duration(seconds)}`."))

    @commands.hybrid_command(name="volume", aliases=["vol"])
    @app_commands.describe(percent="0-200. Leave empty to see the current volume.")
    @commands.guild_only()
    async def volume(self, ctx: commands.Context, percent: int | None = None) -> None:
        """Show or set playback volume."""
        player = self.require_player(ctx)
        if percent is None:
            await ctx.send(embed=embeds.info(f"🔊 Volume is **{player.volume_percent}%**."))
            return

        # Changing volume affects everyone, so it needs the DJ check.
        if not await checks.is_dj().predicate(ctx):
            return
        if not 0 <= percent <= 200:
            raise FriendlyError("Volume must be between 0 and 200.")

        player.set_volume(percent)
        await self.bot.db.set_guild_setting(ctx.guild.id, "music_volume", percent)
        await player.refresh_now_playing()
        await ctx.send(embed=embeds.success(f"🔊 Volume set to **{percent}%**."))

    @commands.hybrid_command(name="loop", aliases=["repeat"])
    @app_commands.describe(mode="What to repeat. Leave empty to cycle through the modes.")
    @commands.guild_only()
    @checks.is_dj()
    async def loop(
        self, ctx: commands.Context, mode: Literal["off", "track", "queue"] | None = None
    ) -> None:
        """Repeat the current track, the whole queue, or nothing."""
        player = self.require_player(ctx)
        player.loop_mode = LoopMode(mode) if mode else player.loop_mode.cycled()
        await player.refresh_now_playing()
        await ctx.send(
            embed=embeds.success(f"{player.loop_mode.emoji} Loop: **{player.loop_mode.label}**.")
        )

    # ── queue management ─────────────────────────────────────────────────────

    @commands.hybrid_command(name="nowplaying", aliases=["np", "current"])
    @commands.guild_only()
    async def nowplaying(self, ctx: commands.Context) -> None:
        """Show what's playing."""
        player = self.require_player(ctx)
        if player.current is None:
            raise FriendlyError("Nothing is playing.")
        await ctx.send(embed=player.now_playing_embed())

    @commands.hybrid_command(name="queue", aliases=["q"])
    @commands.guild_only()
    async def queue_command(self, ctx: commands.Context) -> None:
        """Show the queue."""
        player = self.require_player(ctx)
        tracks = list(player.queue)

        header = ""
        if player.current is not None:
            header = (
                f"**Now playing**\n{player.current.display(70)} "
                f"`{format_duration(player.position)} / {format_duration(player.current.duration)}`\n\n"
            )
        if not tracks:
            await ctx.send(embed=embeds.info(header + "*The queue is empty.*", title="Queue"))
            return

        total = player.queue.total_duration
        pages: list[discord.Embed] = []
        for start in range(0, len(tracks), QUEUE_PAGE_SIZE):
            chunk = tracks[start : start + QUEUE_PAGE_SIZE]
            lines = [
                f"`{start + offset + 1}.` {track.display(55)} "
                f"`{format_duration(track.duration)}` — <@{track.requester_id}>"
                for offset, track in enumerate(chunk)
            ]
            embed = embeds.info(header + "\n".join(lines), title="Queue")
            length = format_duration(total) if total is not None else "some live streams"
            embed.set_footer(
                text=f"{len(tracks)} track{'s' if len(tracks) != 1 else ''} • {length} • "
                f"{player.loop_mode.emoji} loop: {player.loop_mode.label}"
            )
            pages.append(embed)

        await send_pages(ctx, pages)

    @commands.hybrid_command(name="clear")
    @commands.guild_only()
    @checks.is_dj()
    async def clear(self, ctx: commands.Context) -> None:
        """Empty the queue without stopping the current track."""
        player = self.require_player(ctx)
        removed = player.queue.clear()
        await ctx.send(embed=embeds.success(f"Cleared **{removed}** tracks from the queue."))

    @commands.hybrid_command(name="remove", aliases=["rm"])
    @app_commands.describe(index="Queue position to remove, as shown by /queue")
    @commands.guild_only()
    @checks.is_dj()
    async def remove(self, ctx: commands.Context, index: int) -> None:
        """Remove one track from the queue."""
        player = self.require_player(ctx)
        try:
            track = player.queue.remove(index - 1)
        except IndexError as exc:
            raise FriendlyError(f"There's no track at position {index}.") from exc
        await ctx.send(embed=embeds.success(f"Removed **{truncate(track.title, 80)}**."))

    @commands.hybrid_command(name="move")
    @app_commands.describe(source="Track to move", destination="Where to move it")
    @commands.guild_only()
    @checks.is_dj()
    async def move(self, ctx: commands.Context, source: int, destination: int) -> None:
        """Move a track to another position in the queue."""
        player = self.require_player(ctx)
        try:
            track = player.queue.move(source - 1, destination - 1)
        except IndexError as exc:
            raise FriendlyError(f"There's no track at position {source}.") from exc
        await ctx.send(
            embed=embeds.success(f"Moved **{truncate(track.title, 60)}** to position **{destination}**.")
        )

    @commands.hybrid_command(name="shuffle")
    @commands.guild_only()
    @checks.is_dj()
    async def shuffle(self, ctx: commands.Context) -> None:
        """Shuffle the queue."""
        player = self.require_player(ctx)
        if len(player.queue) < 2:
            raise FriendlyError("Not enough tracks queued to shuffle.")
        player.queue.shuffle()
        await ctx.send(embed=embeds.success(f"🔀 Shuffled **{len(player.queue)}** tracks."))

    @commands.hybrid_command(name="dedupe", aliases=["deduplicate"])
    @commands.guild_only()
    @checks.is_dj()
    async def dedupe(self, ctx: commands.Context) -> None:
        """Remove duplicate tracks from the queue."""
        player = self.require_player(ctx)
        removed = player.queue.deduplicate()
        await ctx.send(
            embed=embeds.success(f"Removed **{removed}** duplicate{'s' if removed != 1 else ''}.")
        )

    # ── saved playlists ──────────────────────────────────────────────────────

    @commands.hybrid_group(name="playlist", aliases=["pl"], fallback="list", invoke_without_command=True)
    @commands.guild_only()
    async def playlist(self, ctx: commands.Context) -> None:
        """Save and reload queues."""
        rows = await self.bot.db.fetchall(
            "SELECT p.name, p.owner_id, COUNT(t.id) AS tracks "
            "FROM playlists p LEFT JOIN playlist_tracks t ON t.playlist_id = p.id "
            "WHERE p.guild_id = ? GROUP BY p.id ORDER BY p.name",
            (ctx.guild.id,),
        )
        if not rows:
            await ctx.send(
                embed=embeds.info(
                    f"No saved playlists yet. Queue some music and run "
                    f"`{ctx.clean_prefix}playlist save <name>`."
                )
            )
            return
        lines = [
            f"**{row['name']}** — {row['tracks']} track{'s' if row['tracks'] != 1 else ''}, "
            f"saved by <@{row['owner_id']}>"
            for row in rows
        ]
        await ctx.send(embed=embeds.info("\n".join(lines), title="Saved playlists"))

    @playlist.command(name="save")
    @app_commands.describe(name="Name to save the current queue under")
    async def playlist_save(self, ctx: commands.Context, *, name: str) -> None:
        """Save the current track and queue as a playlist."""
        player = self.require_player(ctx)
        name = name.strip().lower()[:60]
        if not name:
            raise FriendlyError("Give the playlist a name.")

        tracks: list[Track] = ([player.current] if player.current else []) + list(player.queue)
        if not tracks:
            raise FriendlyError("There's nothing playing or queued to save.")

        existing = await self.bot.db.fetchone(
            "SELECT id, owner_id FROM playlists WHERE guild_id = ? AND name = ?", (ctx.guild.id, name)
        )
        if existing is not None:
            if existing["owner_id"] != ctx.author.id and not ctx.author.guild_permissions.manage_guild:
                raise FriendlyError(f"**{name}** belongs to someone else. Pick another name.")
            await self.bot.db.execute("DELETE FROM playlists WHERE id = ?", (existing["id"],))

        cursor = await self.bot.db.execute(
            "INSERT INTO playlists (guild_id, owner_id, name, created_at) VALUES (?, ?, ?, ?)",
            (ctx.guild.id, ctx.author.id, name, time.time()),
        )
        playlist_id = cursor.lastrowid
        await self.bot.db.executemany(
            "INSERT INTO playlist_tracks (playlist_id, position, title, url, duration) VALUES (?, ?, ?, ?, ?)",
            [
                (playlist_id, position, track.title, track.url or track.title, track.duration)
                for position, track in enumerate(tracks)
            ],
        )
        await ctx.send(
            embed=embeds.success(f"Saved **{len(tracks)}** tracks as **{name}**.")
        )

    @playlist.command(name="load")
    @app_commands.describe(name="Playlist to queue up")
    async def playlist_load(self, ctx: commands.Context, *, name: str) -> None:
        """Add a saved playlist to the queue."""
        name = name.strip().lower()
        row = await self.bot.db.fetchone(
            "SELECT id FROM playlists WHERE guild_id = ? AND name = ?", (ctx.guild.id, name)
        )
        if row is None:
            raise FriendlyError(f"No playlist named **{name}**.")

        saved = await self.bot.db.fetchall(
            "SELECT title, url, duration FROM playlist_tracks WHERE playlist_id = ? ORDER BY position",
            (row["id"],),
        )
        if not saved:
            raise FriendlyError(f"**{name}** is empty.")

        await ctx.defer()
        player = await self.ensure_player(ctx)
        added = player.queue.extend(
            Track(
                title=item["title"],
                url=item["url"],
                requester_id=ctx.author.id,
                duration=item["duration"],
            )
            for item in saved
        )
        await ctx.send(embed=embeds.success(f"Queued **{added}** tracks from **{name}**."))

    @playlist.command(name="delete", aliases=["remove"])
    @app_commands.describe(name="Playlist to delete")
    async def playlist_delete(self, ctx: commands.Context, *, name: str) -> None:
        """Delete a saved playlist."""
        name = name.strip().lower()
        row = await self.bot.db.fetchone(
            "SELECT id, owner_id FROM playlists WHERE guild_id = ? AND name = ?", (ctx.guild.id, name)
        )
        if row is None:
            raise FriendlyError(f"No playlist named **{name}**.")
        if row["owner_id"] != ctx.author.id and not ctx.author.guild_permissions.manage_guild:
            raise FriendlyError("You can only delete playlists you saved.")
        await self.bot.db.execute("DELETE FROM playlists WHERE id = ?", (row["id"],))
        await ctx.send(embed=embeds.success(f"Deleted **{name}**."))


class SearchView(discord.ui.View):
    """Result picker for `/search`."""

    def __init__(self, cog: Music, ctx: commands.Context, tracks: list[Track]) -> None:
        super().__init__(timeout=60.0)
        self.cog = cog
        self.ctx = ctx
        self.tracks = tracks
        self.message: discord.Message | None = None

        self.select.options = [
            discord.SelectOption(
                label=truncate(track.title, 100),
                description=f"{format_duration(track.duration)} • {truncate(track.uploader or 'Unknown', 60)}",
                value=str(index),
            )
            for index, track in enumerate(tracks)
        ]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                embed=embeds.error("Run `/search` yourself to pick a result."), ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    @discord.ui.select(placeholder="Pick a track…")
    async def select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        track = self.tracks[int(select.values[0])]
        await interaction.response.defer()
        try:
            player = await self.cog.ensure_player(self.ctx)
        except FriendlyError as exc:
            await interaction.followup.send(embed=embeds.error(str(exc)), ephemeral=True)
            return

        if player.queue.free_slots <= 0:
            await interaction.followup.send(embed=embeds.error("The queue is full."), ephemeral=True)
            return

        player.queue.put(track)
        self.stop()
        if self.message is not None:
            try:
                await self.message.edit(
                    embed=embeds.success(f"Added {track.display(70)} to the queue."), view=None
                )
            except discord.HTTPException:
                pass


async def setup(bot) -> None:
    await bot.add_cog(Music(bot))
