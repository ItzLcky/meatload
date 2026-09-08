import asyncio
import unittest

from bot.music.queue import LoopMode, TrackQueue
from bot.music.source import Track


def track(name: str, duration: int | None = 100, requester: int = 1) -> Track:
    return Track(title=name, url=f"https://example.com/{name}", requester_id=requester, duration=duration)


class TestTrackQueue(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.queue = TrackQueue(maxsize=5)

    async def test_fifo_order(self):
        for name in "abc":
            self.queue.put(track(name))
        self.assertEqual([(await self.queue.get()).title for _ in range(3)], ["a", "b", "c"])

    async def test_put_front_jumps_the_line(self):
        self.queue.put(track("a"))
        self.queue.put_front(track("b"))
        self.assertEqual((await self.queue.get()).title, "b")

    async def test_get_waits_for_a_track(self):
        async def add_later():
            await asyncio.sleep(0.05)
            self.queue.put(track("late"))

        asyncio.create_task(add_later())
        self.assertEqual((await self.queue.get(timeout=2)).title, "late")

    async def test_get_times_out_when_nothing_arrives(self):
        with self.assertRaises(asyncio.TimeoutError):
            await self.queue.get(timeout=0.05)

    async def test_extend_respects_maxsize(self):
        added = self.queue.extend(track(str(index)) for index in range(10))
        self.assertEqual(added, 5)
        self.assertEqual(len(self.queue), 5)
        self.assertEqual(self.queue.free_slots, 0)

    async def test_remove_by_index(self):
        for name in "abc":
            self.queue.put(track(name))
        self.assertEqual(self.queue.remove(1).title, "b")
        self.assertEqual([item.title for item in self.queue], ["a", "c"])

    async def test_remove_out_of_range(self):
        self.queue.put(track("a"))
        for index in (-1, 1, 99):
            with self.subTest(index=index), self.assertRaises(IndexError):
                self.queue.remove(index)

    async def test_move(self):
        for name in "abcd":
            self.queue.put(track(name))
        self.queue.move(0, 2)
        self.assertEqual([item.title for item in self.queue], ["b", "c", "a", "d"])

    async def test_move_clamps_destination(self):
        for name in "abc":
            self.queue.put(track(name))
        self.queue.move(0, 99)
        self.assertEqual([item.title for item in self.queue], ["b", "c", "a"])

    async def test_total_duration(self):
        self.queue.put(track("a", 100))
        self.queue.put(track("b", 50))
        self.assertEqual(self.queue.total_duration, 150)

    async def test_total_duration_is_unknown_with_a_live_stream(self):
        self.queue.put(track("a", 100))
        self.queue.put(track("live", None))
        self.assertIsNone(self.queue.total_duration)

    async def test_deduplicate_keeps_first_occurrence(self):
        self.queue.put(track("a"))
        self.queue.put(track("b"))
        self.queue.put(track("a"))
        self.assertEqual(self.queue.deduplicate(), 1)
        self.assertEqual([item.title for item in self.queue], ["a", "b"])

    async def test_remove_by_requester(self):
        self.queue.put(track("a", requester=1))
        self.queue.put(track("b", requester=2))
        self.queue.put(track("c", requester=1))
        self.assertEqual(self.queue.remove_by_requester(1), 2)
        self.assertEqual([item.title for item in self.queue], ["b"])

    async def test_clear(self):
        for name in "abc":
            self.queue.put(track(name))
        self.assertEqual(self.queue.clear(), 3)
        with self.assertRaises(asyncio.TimeoutError):
            await self.queue.get(timeout=0.05)

    async def test_shuffle_preserves_membership(self):
        for name in "abcde":
            self.queue.put(track(name))
        self.queue.shuffle()
        self.assertEqual(sorted(item.title for item in self.queue), list("abcde"))

    async def test_drained_queue_blocks_again(self):
        """Regression: the internal event must be cleared once the queue empties."""
        self.queue.put(track("a"))
        await self.queue.get()
        with self.assertRaises(asyncio.TimeoutError):
            await self.queue.get(timeout=0.05)


class TestLoopMode(unittest.TestCase):
    def test_cycles_through_all_three(self):
        mode = LoopMode.OFF
        seen = []
        for _ in range(3):
            mode = mode.cycled()
            seen.append(mode)
        self.assertEqual(seen, [LoopMode.TRACK, LoopMode.QUEUE, LoopMode.OFF])


if __name__ == "__main__":
    unittest.main()
