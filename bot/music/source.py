"""Track resolution: yt-dlp extraction plus optional Spotify link expansion.

Resolution is deliberately two-phase:

1. `resolve_query` runs a *flat* extraction. It is fast even for a 200-track
   playlist because it never touches individual videos, and it is what runs
   while the user is waiting on `/play`.
2. `resolve_stream` runs a full extraction for one track, immediately before it
   plays. Media URLs are time-limited, so resolving them at queue time would
   hand us links that expire before a long queue reaches them.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import yt_dlp

log = logging.getLogger(__name__)

_SPOTIFY_RE = re.compile(
    r"(?:https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?|spotify:)(track|album|playlist)[/:]([A-Za-z0-9]+)"
)
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# Media URLs stay valid for hours, but re-resolving anything older than this is
# cheap insurance against a track that sat in a long queue.
STREAM_TTL_SECONDS = 20 * 60


class ExtractionError(Exception):
    """A user-facing failure to turn a query into playable audio."""


@dataclass(slots=True)
class Track:
    title: str
    url: str
    requester_id: int
    duration: int | None = None
    thumbnail: str | None = None
    uploader: str | None = None
    is_live: bool = False
    # Set for tracks that came from Spotify metadata: there is no playable
    # Spotify audio, so this is the phrase we search for at play time.
    search_query: str | None = None
    stream_url: str | None = field(default=None, compare=False)
    _resolved_at: float = field(default=0.0, compare=False)

    @property
    def needs_resolution(self) -> bool:
        return self.stream_url is None or (time.time() - self._resolved_at) > STREAM_TTL_SECONDS

    def display(self, limit: int = 60) -> str:
        title = self.title if len(self.title) <= limit else self.title[: limit - 1] + "…"
        return f"[{title}]({self.url})" if _URL_RE.match(self.url) else title


@dataclass(slots=True)
class Resolution:
    tracks: list[Track]
    playlist_title: str | None = None
    source_name: str = "YouTube"


def _friendly_error(message: str) -> str:
    """Translate yt-dlp's noisiest failures into something actionable."""
    lowered = message.lower()
    if "sign in to confirm" in lowered or "not a bot" in lowered:
        return (
            "YouTube is asking this bot to prove it isn't a bot. Export cookies from a "
            "browser to `cookies/cookies.txt` and set `YTDLP_COOKIES_FILE` in `.env`, "
            "then restart the bot."
        )
    if "private video" in lowered:
        return "That video is private."
    if "video unavailable" in lowered or "unavailable" in lowered:
        return "That video is unavailable (deleted, region-locked, or removed)."
    if "age" in lowered and "restrict" in lowered:
        return "That video is age-restricted. A `cookies.txt` from a logged-in account is needed to play it."
    if "members-only" in lowered or "join this channel" in lowered:
        return "That video is members-only."
    if "unsupported url" in lowered:
        return "I don't know how to play that link."
    if "no video formats" in lowered or "requested format" in lowered:
        return (
            "No playable audio was found for that link. If this started happening to "
            "everything, yt-dlp is probably out of date — restart the bot with "
            "`YTDLP_AUTO_UPDATE=true`."
        )
    # Strip yt-dlp's "ERROR: [youtube] abc123:" prefix, which means nothing to a user.
    cleaned = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*)?(\S+:\s*)?", "", message).strip()
    return cleaned or "Extraction failed."


class SpotifyClient:
    """Minimal client-credentials Spotify reader (metadata only)."""

    TOKEN_URL = "https://accounts.spotify.com/api/token"
    API = "https://api.spotify.com/v1"

    def __init__(self, session: aiohttp.ClientSession, client_id: str, client_secret: str) -> None:
        self._session = session
        self._auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def _get_token(self) -> str:
        async with self._lock:
            if self._token and time.time() < self._expires_at - 60:
                return self._token
            async with self._session.post(
                self.TOKEN_URL,
                data={"grant_type": "client_credentials"},
                headers={"Authorization": f"Basic {self._auth}"},
            ) as response:
                if response.status != 200:
                    raise ExtractionError(
                        "Spotify rejected the configured credentials. Check "
                        "`SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`."
                    )
                payload = await response.json()
            self._token = payload["access_token"]
            self._expires_at = time.time() + payload.get("expires_in", 3600)
            return self._token

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        token = await self._get_token()
        async with self._session.get(
            f"{self.API}{path}", params=params, headers={"Authorization": f"Bearer {token}"}
        ) as response:
            if response.status == 404:
                raise ExtractionError("That Spotify link doesn't exist or isn't public.")
            if response.status != 200:
                raise ExtractionError(f"Spotify returned HTTP {response.status}.")
            return await response.json()

    @staticmethod
    def _phrase(track: dict[str, Any]) -> tuple[str, str]:
        artists = ", ".join(artist["name"] for artist in track.get("artists", []) if artist.get("name"))
        name = track.get("name", "Unknown")
        title = f"{artists} - {name}" if artists else name
        return title, f"{title} audio"

    async def resolve(self, kind: str, spotify_id: str, requester_id: int, limit: int) -> Resolution:
        if kind == "track":
            data = await self._get(f"/tracks/{spotify_id}")
            title, query = self._phrase(data)
            return Resolution(
                tracks=[
                    Track(
                        title=title,
                        url=data.get("external_urls", {}).get("spotify", ""),
                        requester_id=requester_id,
                        duration=(data.get("duration_ms") or 0) // 1000 or None,
                        search_query=query,
                    )
                ],
                source_name="Spotify",
            )

        if kind == "album":
            album = await self._get(f"/albums/{spotify_id}")
            playlist_title = album.get("name")
            items = album.get("tracks", {}).get("items", [])
            next_url = album.get("tracks", {}).get("next")
            raw = list(items)
        else:
            playlist = await self._get(f"/playlists/{spotify_id}")
            playlist_title = playlist.get("name")
            page = playlist.get("tracks", {})
            raw = [item.get("track") for item in page.get("items", []) if item.get("track")]
            next_url = page.get("next")

        # Both endpoints paginate identically; keep pulling until we hit `limit`.
        while next_url and len(raw) < limit:
            token = await self._get_token()
            async with self._session.get(next_url, headers={"Authorization": f"Bearer {token}"}) as response:
                if response.status != 200:
                    break
                page = await response.json()
            for item in page.get("items", []):
                entry = item.get("track") if kind == "playlist" else item
                if entry:
                    raw.append(entry)
            next_url = page.get("next")

        tracks: list[Track] = []
        for entry in raw[:limit]:
            if not entry or entry.get("is_local"):
                continue
            title, query = self._phrase(entry)
            tracks.append(
                Track(
                    title=title,
                    url=entry.get("external_urls", {}).get("spotify", ""),
                    requester_id=requester_id,
                    duration=(entry.get("duration_ms") or 0) // 1000 or None,
                    search_query=query,
                )
            )

        if not tracks:
            raise ExtractionError("That Spotify link has no playable tracks.")
        return Resolution(tracks=tracks, playlist_title=playlist_title, source_name="Spotify")


class AudioResolver:
    """Wraps yt-dlp so extraction never blocks the event loop."""

    BASE_OPTS: dict[str, Any] = {
        "format": "bestaudio[abr<=160]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logtostderr": False,
        "ignoreerrors": False,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "retries": 5,
        "extractor_retries": 3,
        "socket_timeout": 20,
        "default_search": "ytsearch",
        # YouTube blocks a lot of IPv6 ranges used by hosting providers;
        # forcing IPv4 avoids a common and very confusing failure mode.
        "source_address": "0.0.0.0",
    }

    def __init__(self, config, session: aiohttp.ClientSession) -> None:
        self.config = config
        self.session = session
        self.spotify: SpotifyClient | None = None
        if config.spotify_enabled:
            self.spotify = SpotifyClient(session, config.spotify_client_id, config.spotify_client_secret)

    def _opts(self, **overrides: Any) -> dict[str, Any]:
        opts = dict(self.BASE_OPTS)
        if self.config.ytdlp_cookies_file:
            opts["cookiefile"] = self.config.ytdlp_cookies_file
        if self.config.ytdlp_extractor_args:
            opts["extractor_args"] = _parse_extractor_args(self.config.ytdlp_extractor_args)
        opts.update(overrides)
        return opts

    async def _extract(self, query: str, **overrides: Any) -> dict[str, Any]:
        opts = self._opts(**overrides)

        def run() -> dict[str, Any]:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(query, download=False)

        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(None, functools.partial(run))
        except yt_dlp.utils.DownloadError as exc:
            log.warning("yt-dlp failed for %r: %s", query, exc)
            raise ExtractionError(_friendly_error(str(exc))) from exc
        except Exception as exc:  # noqa: BLE001 - surface anything as a clean message
            log.exception("Unexpected extraction failure for %r", query)
            raise ExtractionError(f"Extraction failed: {exc}") from exc

        if data is None:
            raise ExtractionError("Nothing was found for that.")
        return data

    # ── phase 1: query -> track list ─────────────────────────────────────────

    async def resolve_query(self, query: str, requester_id: int) -> Resolution:
        query = query.strip()
        if not query:
            raise ExtractionError("Give me something to play.")

        spotify_match = _SPOTIFY_RE.search(query)
        if spotify_match:
            if self.spotify is None:
                raise ExtractionError(
                    "Spotify links need `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` set in `.env`. "
                    "Search for the song by name instead, or paste a YouTube link."
                )
            kind, spotify_id = spotify_match.group(1), spotify_match.group(2)
            return await self.spotify.resolve(kind, spotify_id, requester_id, self.config.music_max_playlist)

        is_url = bool(_URL_RE.match(query))
        target = query if is_url else f"ytsearch1:{query}"
        allow_playlist = is_url and _is_playlist_url(query)

        data = await self._extract(
            target,
            noplaylist=not allow_playlist,
            # Flat extraction keeps a big playlist near-instant; individual
            # tracks get fully resolved right before they play.
            extract_flat="in_playlist",
        )

        if data.get("_type") == "playlist":
            entries = [entry for entry in (data.get("entries") or []) if entry]
            if not entries:
                raise ExtractionError("That link has no playable entries.")
            limit = self.config.music_max_playlist if allow_playlist else 1
            tracks = [self._track_from(entry, requester_id) for entry in entries[:limit]]
            playlist_title = data.get("title") if allow_playlist else None
            return Resolution(tracks=tracks, playlist_title=playlist_title, source_name=data.get("extractor_key", "YouTube"))

        return Resolution(
            tracks=[self._track_from(data, requester_id)],
            source_name=data.get("extractor_key", "YouTube"),
        )

    def _track_from(self, data: dict[str, Any], requester_id: int) -> Track:
        url = data.get("webpage_url") or data.get("original_url") or data.get("url") or ""
        duration = data.get("duration")
        track = Track(
            title=data.get("title") or "Unknown title",
            url=url,
            requester_id=requester_id,
            duration=int(duration) if duration else None,
            thumbnail=_pick_thumbnail(data),
            uploader=data.get("uploader") or data.get("channel") or data.get("uploader_id"),
            is_live=bool(data.get("is_live")),
        )
        # A non-flat extraction already handed us a media URL; keep it so an
        # immediate `/play` doesn't pay for a second round trip.
        if not data.get("_type") and data.get("url") and data.get("url") != url:
            track.stream_url = data["url"]
            track._resolved_at = time.time()
        return track

    # ── phase 2: track -> media URL ──────────────────────────────────────────

    async def resolve_stream(self, track: Track) -> None:
        """Fill in `track.stream_url`, searching first for Spotify-sourced tracks."""
        if not track.needs_resolution:
            return

        target = f"ytsearch1:{track.search_query}" if track.search_query else track.url
        if not target:
            raise ExtractionError(f"No playable source for **{track.title}**.")

        data = await self._extract(target, noplaylist=True, extract_flat=False)

        if data.get("_type") == "playlist":
            entries = [entry for entry in (data.get("entries") or []) if entry]
            if not entries:
                raise ExtractionError(f"Couldn't find audio for **{track.title}**.")
            data = entries[0]

        stream_url = data.get("url")
        if not stream_url:
            formats = data.get("requested_formats") or []
            stream_url = formats[0].get("url") if formats else None
        if not stream_url:
            raise ExtractionError(f"Couldn't get an audio stream for **{track.title}**.")

        track.stream_url = stream_url
        track._resolved_at = time.time()
        track.is_live = bool(data.get("is_live"))
        # Spotify tracks arrive with only metadata; adopt the real source now.
        if track.search_query:
            track.url = data.get("webpage_url") or track.url
            track.uploader = data.get("uploader") or track.uploader
        if track.duration is None and data.get("duration"):
            track.duration = int(data["duration"])
        if not track.thumbnail:
            track.thumbnail = _pick_thumbnail(data)


def _pick_thumbnail(data: dict[str, Any]) -> str | None:
    if data.get("thumbnail"):
        return data["thumbnail"]
    thumbnails = data.get("thumbnails") or []
    return thumbnails[-1].get("url") if thumbnails else None


def _is_playlist_url(url: str) -> bool:
    """Whether a link should expand into many tracks.

    A YouTube `watch?v=...&list=...` link is treated as the single video it
    points at: those come from clicking a video *inside* a playlist, and
    dumping 200 tracks into the queue is rarely what the sender meant. Paste
    the `/playlist?list=...` link to queue the whole thing.
    """
    lowered = url.lower()
    if "youtube.com" in lowered or "youtu.be" in lowered:
        return "list=" in lowered and "v=" not in lowered
    return any(part in lowered for part in ("/playlist", "/album", "/sets/", "/sets?"))


def _parse_extractor_args(raw: str) -> dict[str, dict[str, list[str]]]:
    """Parse `youtube:player_client=web_safari,default;other:key=v` into yt-dlp's shape."""
    parsed: dict[str, dict[str, list[str]]] = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        extractor, _, rest = chunk.partition(":")
        key, _, values = rest.partition("=")
        if not key:
            continue
        parsed.setdefault(extractor.strip(), {})[key.strip()] = [
            value.strip() for value in values.split(",") if value.strip()
        ]
    return parsed
