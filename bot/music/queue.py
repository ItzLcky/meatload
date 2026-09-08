"""The per-guild track queue."""

from __future__ import annotations

import asyncio
import enum
import random
from collections import deque
from typing import Iterable, Iterator

from .source import Track


class LoopMode(enum.Enum):
    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"

    @property
    def label(self) -> str:
        return {"off": "Off", "track": "Track", "queue": "Queue"}[self.value]

    @property
    def emoji(self) -> str:
        return {"off": "➡️", "track": "🔂", "queue": "🔁"}[self.value]

    def cycled(self) -> "LoopMode":
        order = [LoopMode.OFF, LoopMode.TRACK, LoopMode.QUEUE]
        return order[(order.index(self) + 1) % len(order)]


class TrackQueue:
    """An awaitable FIFO of tracks with the list operations the cog needs.

    `asyncio.Queue` would cover the awaiting half but not indexing, moving, or
    shuffling, which the queue commands all need.
    """

    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self._items: deque[Track] = deque()
        self._available = asyncio.Event()

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[Track]:
        return iter(self._items)

    def __getitem__(self, index: int) -> Track:
        return self._items[index]

    @property
    def free_slots(self) -> int:
        return max(0, self.maxsize - len(self._items))

    @property
    def total_duration(self) -> int | None:
        """Total runtime, or None if anything in the queue is a live stream."""
        total = 0
        for track in self._items:
            if track.duration is None:
                return None
            total += track.duration
        return total

    def put(self, track: Track) -> None:
        self._items.append(track)
        self._available.set()

    def put_front(self, track: Track) -> None:
        self._items.appendleft(track)
        self._available.set()

    def extend(self, tracks: Iterable[Track]) -> int:
        added = 0
        for track in tracks:
            if len(self._items) >= self.maxsize:
                break
            self._items.append(track)
            added += 1
        if added:
            self._available.set()
        return added

    async def get(self, timeout: float | None = None) -> Track:
        """Pop the next track, waiting up to `timeout` seconds for one to arrive.

        Raises `asyncio.TimeoutError` when the wait elapses, which the player
        treats as "idle long enough, leave the channel".
        """
        while True:
            if self._items:
                track = self._items.popleft()
                if not self._items:
                    self._available.clear()
                return track
            self._available.clear()
            await asyncio.wait_for(self._available.wait(), timeout=timeout)

    def clear(self) -> int:
        count = len(self._items)
        self._items.clear()
        self._available.clear()
        return count

    def shuffle(self) -> None:
        items = list(self._items)
        random.shuffle(items)
        self._items = deque(items)

    def remove(self, index: int) -> Track:
        """Remove by zero-based index; raises IndexError if out of range."""
        if not 0 <= index < len(self._items):
            raise IndexError(index)
        self._items.rotate(-index)
        track = self._items.popleft()
        self._items.rotate(index)
        if not self._items:
            self._available.clear()
        return track

    def move(self, source: int, destination: int) -> Track:
        track = self.remove(source)
        destination = max(0, min(destination, len(self._items)))
        items = list(self._items)
        items.insert(destination, track)
        self._items = deque(items)
        self._available.set()
        return track

    def remove_by_requester(self, user_id: int) -> int:
        remaining = deque(track for track in self._items if track.requester_id != user_id)
        removed = len(self._items) - len(remaining)
        self._items = remaining
        if not self._items:
            self._available.clear()
        return removed

    def deduplicate(self) -> int:
        seen: set[str] = set()
        remaining: deque[Track] = deque()
        for track in self._items:
            key = track.url or track.title
            if key in seen:
                continue
            seen.add(key)
            remaining.append(track)
        removed = len(self._items) - len(remaining)
        self._items = remaining
        return removed
