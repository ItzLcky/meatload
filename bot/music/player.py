"""Per-guild playback: one asyncio task drives one voice connection."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import TYPE_CHECKING

import discord

from ..utils import embeds
from .queue import LoopMode, TrackQueue
from .source import ExtractionError, Track

if TYPE_CHECKING:
    from ..cogs.music import Music

log = logging.getLogger(__name__)

# `-reconnect*` matters because these are long-lived HTTP streams and a single
# dropped connection would otherwise end the track silently.
FFMPEG_BEFORE = "-nostdin -loglevel error -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"

MAX_HISTORY = 50


class GuildPlayer:
    def __init__(
        self,
        cog: "Music",
        guild: discord.Guild,
        text_channel: discord.abc.Messageable,
        *,
        volume: int,
    ) -> None:
        self.cog = cog
        self.bot = cog.bot
        self.guild = guild
        self.text_channel = text_channel
        self.resolver = cog.resolver

        self.queue = TrackQueue(cog.bot.config.music_max_queue)
        self.history: deque[Track] = deque(maxlen=MAX_HISTORY)
        self.current: Track | None = None
        self.loop_mode = LoopMode.OFF
        self._volume = max(0.0, min(2.0, volume / 100))

        self._task: asyncio.Task | None = None
        self._next = asyncio.Event()
        self._closing = False
        self._destroyed = False
        self._skip_requested = False
        self._suppress_history = False
        self._pending_seek: float | None = None
        self._now_playing_message: discord.Message | None = None

        # Playback clock, used for the seek bar and for `/seek`.
        self._position_base = 0.0
        self._play_started: float | None = None
        self._paused_total = 0.0
        self._paused_at: float | None = None

        self.alone_since: float | None = None

    # ── state ────────────────────────────────────────────────────────────────

    @property
    def voice(self) -> discord.VoiceClient | None:
        client = self.guild.voice_client
        return client if isinstance(client, discord.VoiceClient) else None

    @property
    def channel(self) -> discord.VoiceChannel | discord.StageChannel | None:
        voice = self.voice
        return voice.channel if voice else None

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def volume_percent(self) -> int:
        return round(self._volume * 100)

    @property
    def is_playing(self) -> bool:
        voice = self.voice
        return bool(voice and voice.is_playing())

    @property
    def is_paused(self) -> bool:
        voice = self.voice
        return bool(voice and voice.is_paused())

    @property
    def position(self) -> float:
        """Seconds into the current track, accounting for seeks and pauses."""
        if self._play_started is None:
            return 0.0
        end = self._paused_at if self._paused_at is not None else time.monotonic()
        return self._position_base + max(0.0, end - self._play_started - self._paused_total)

    def _mark_started(self, start_at: float) -> None:
        self._position_base = start_at
        self._play_started = time.monotonic()
        self._paused_total = 0.0
        self._paused_at = None

    # ── controls ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = self.bot.loop.create_task(self._run(), name=f"player-{self.guild.id}")

    def set_volume(self, percent: int) -> None:
        self._volume = max(0.0, min(2.0, percent / 100))
        voice = self.voice
        if voice and isinstance(voice.source, discord.PCMVolumeTransformer):
            voice.source.volume = self._volume

    def pause(self) -> bool:
        voice = self.voice
        if voice and voice.is_playing():
            voice.pause()
            self._paused_at = time.monotonic()
            return True
        return False

    def resume(self) -> bool:
        voice = self.voice
        if voice and voice.is_paused():
            voice.resume()
            if self._paused_at is not None:
                self._paused_total += time.monotonic() - self._paused_at
                self._paused_at = None
            return True
        return False

    def skip(self) -> Track | None:
        voice = self.voice
        if voice is None or not (voice.is_playing() or voice.is_paused()):
            return None
        skipped = self.current
        self._skip_requested = True
        voice.stop()  # fires the `after` callback, which wakes the player loop
        return skipped

    def seek(self, seconds: float) -> None:
        voice = self.voice
        if voice is None or self.current is None:
            raise RuntimeError("nothing is playing")
        if self.current.is_live:
            raise RuntimeError("live streams cannot be seeked")
        self._pending_seek = max(0.0, seconds)
        voice.stop()

    def play_previous(self) -> Track | None:
        """Requeue the last finished track and jump back to it."""
        if not self.history:
            return None
        track = self.history.popleft()
        if self.current is not None:
            self.queue.put_front(self.current)
        self.queue.put_front(track)
        self._skip_requested = True
        self._suppress_history = True
        voice = self.voice
        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()
        return track

    # ── player loop ──────────────────────────────────────────────────────────

    def _after(self, error: Exception | None) -> None:
        # Runs on the voice thread, so hop back to the event loop.
        if error is not None:
            log.warning("Playback error in guild %s: %s", self.guild.id, error)
        self.bot.loop.call_soon_threadsafe(self._next.set)

    def _make_source(self, track: Track, start_at: float) -> discord.AudioSource:
        before = FFMPEG_BEFORE
        if start_at > 0:
            # `-ss` before `-i` makes ffmpeg seek by keyframe without decoding
            # everything up to that point.
            before = f"{before} -ss {start_at:.3f}"
        audio = discord.FFmpegPCMAudio(track.stream_url, before_options=before, options=FFMPEG_OPTIONS)
        return discord.PCMVolumeTransformer(audio, volume=self._volume)

    async def _advance(self) -> Track | None:
        previous = self.current
        self.current = None

        if previous is not None and not self._suppress_history:
            self.history.appendleft(previous)
        self._suppress_history = False

        if previous is not None:
            if self.loop_mode is LoopMode.TRACK and not self._skip_requested:
                self._skip_requested = False
                return previous
            if self.loop_mode is LoopMode.QUEUE:
                self.queue.put(previous)
        self._skip_requested = False

        try:
            return await self.queue.get(timeout=self.bot.config.music_idle_timeout)
        except asyncio.TimeoutError:
            return None

    async def _run(self) -> None:
        try:
            while not self._closing:
                self._next.clear()

                if self._pending_seek is not None:
                    track, start_at = self.current, self._pending_seek
                    self._pending_seek = None
                    if track is None:
                        continue
                else:
                    track = await self._advance()
                    if track is None:
                        await self.notify(
                            embeds.neutral("Nothing queued for a while — leaving the voice channel. 👋")
                        )
                        break
                    start_at = 0.0

                try:
                    await self.resolver.resolve_stream(track)
                except ExtractionError as exc:
                    await self.notify(embeds.error(f"Skipping **{track.title}** — {exc}"))
                    continue

                voice = self.voice
                if voice is None or not voice.is_connected():
                    log.info("Voice connection gone for guild %s, stopping player", self.guild.id)
                    break

                self.current = track
                try:
                    voice.play(self._make_source(track, start_at), after=self._after)
                except discord.ClientException as exc:
                    log.warning("Could not start playback in guild %s: %s", self.guild.id, exc)
                    await self.notify(embeds.error(f"Couldn't start **{track.title}**: {exc}"))
                    # Drop it rather than leaving it as `current`, or a loop mode
                    # would keep retrying the same unplayable track forever.
                    self.current = None
                    continue

                self._mark_started(start_at)
                if start_at == 0.0:
                    await self._send_now_playing(track)

                await self._next.wait()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a crash here must not leave a zombie voice client
            log.exception("Player loop crashed in guild %s", self.guild.id)
            await self.notify(embeds.error("The music player hit an internal error and stopped."))

        await self.destroy()

    # ── messaging ────────────────────────────────────────────────────────────

    async def notify(self, embed: discord.Embed) -> None:
        try:
            await self.text_channel.send(embed=embed)
        except (discord.HTTPException, AttributeError):
            pass

    def now_playing_embed(self) -> discord.Embed:
        from ..utils.formatting import format_duration, progress_bar

        track = self.current
        if track is None:
            return embeds.neutral("Nothing is playing.")

        embed = discord.Embed(
            title=track.title,
            url=track.url or None,
            color=embeds.BLURPLE,
        )
        embed.set_author(name="Paused" if self.is_paused else "Now playing")
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)

        position = self.position
        if track.is_live or not track.duration:
            embed.description = "🔴 **Live stream**"
        else:
            embed.description = (
                f"`{format_duration(position)}` {progress_bar(position, track.duration)} "
                f"`{format_duration(track.duration)}`"
            )

        if track.uploader:
            embed.add_field(name="Uploader", value=track.uploader, inline=True)
        embed.add_field(name="Requested by", value=f"<@{track.requester_id}>", inline=True)
        embed.add_field(name="Volume", value=f"{self.volume_percent}%", inline=True)

        footer = f"{self.loop_mode.emoji} Loop: {self.loop_mode.label}"
        if len(self.queue):
            footer += f"  •  {len(self.queue)} track{'s' if len(self.queue) != 1 else ''} queued"
        embed.set_footer(text=footer)
        return embed

    async def _send_now_playing(self, track: Track) -> None:
        from .views import PlayerControls

        await self._clear_now_playing()
        try:
            view = PlayerControls(self)
            self._now_playing_message = await self.text_channel.send(
                embed=self.now_playing_embed(), view=view
            )
            view.message = self._now_playing_message
        except (discord.HTTPException, AttributeError):
            self._now_playing_message = None

    async def refresh_now_playing(self) -> None:
        """Re-render the controls message in place after a state change."""
        if self._now_playing_message is None:
            return
        try:
            await self._now_playing_message.edit(embed=self.now_playing_embed())
        except discord.HTTPException:
            self._now_playing_message = None

    async def _clear_now_playing(self) -> None:
        message, self._now_playing_message = self._now_playing_message, None
        if message is None:
            return
        try:
            await message.edit(view=None)
        except discord.HTTPException:
            pass

    # ── teardown ─────────────────────────────────────────────────────────────

    async def destroy(self) -> None:
        """Idempotent teardown: stop audio, leave the channel, drop the player."""
        if self._destroyed:
            return
        self._destroyed = True
        self._closing = True

        self.queue.clear()
        self.current = None

        task = self._task
        if task is not None and task is not asyncio.current_task():
            task.cancel()

        voice = self.voice
        if voice is not None:
            try:
                if voice.is_playing() or voice.is_paused():
                    voice.stop()
                await voice.disconnect(force=True)
            except Exception:  # noqa: BLE001
                log.debug("Ignoring error while disconnecting from guild %s", self.guild.id, exc_info=True)

        await self._clear_now_playing()
        self.cog.players.pop(self.guild.id, None)
