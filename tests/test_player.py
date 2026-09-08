"""The player's track-advance logic and playback clock.

`_advance` decides what plays next from the loop mode, whether a skip was
requested, and the history. It is the fiddliest logic in the bot, so it gets
exercised directly against fakes rather than a live voice connection.
"""

import asyncio
import types
import unittest

from bot.music.player import GuildPlayer
from bot.music.queue import LoopMode
from bot.music.source import Track


def make_player(idle_timeout: float = 0.05) -> GuildPlayer:
    config = types.SimpleNamespace(
        music_max_queue=100,
        music_idle_timeout=idle_timeout,
        music_default_volume=50,
    )
    # Only start()/the `after` callback need a loop, and these tests hit neither.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    bot = types.SimpleNamespace(config=config, loop=loop)
    cog = types.SimpleNamespace(bot=bot, resolver=None, players={})
    guild = types.SimpleNamespace(id=1, voice_client=None)
    return GuildPlayer(cog, guild, text_channel=None, volume=50)


def track(name: str) -> Track:
    return Track(title=name, url=f"https://example.com/{name}", requester_id=1, duration=100)


class TestAdvance(unittest.IsolatedAsyncioTestCase):
    async def test_plays_the_queue_in_order(self):
        player = make_player()
        player.queue.put(track("a"))
        player.queue.put(track("b"))
        self.assertEqual((await player._advance()).title, "a")
        player.current = track("a")
        self.assertEqual((await player._advance()).title, "b")

    async def test_returns_none_when_idle_long_enough(self):
        """This is what makes the bot leave an empty channel."""
        player = make_player(idle_timeout=0.05)
        self.assertIsNone(await player._advance())

    async def test_finished_track_goes_to_history(self):
        player = make_player()
        player.queue.put(track("next"))
        player.current = track("done")
        await player._advance()
        self.assertEqual([item.title for item in player.history], ["done"])

    async def test_loop_track_repeats_the_same_track(self):
        player = make_player()
        player.loop_mode = LoopMode.TRACK
        player.current = track("a")
        self.assertEqual((await player._advance()).title, "a")

    async def test_skip_escapes_loop_track(self):
        """Otherwise `/skip` would be a no-op while looping one track."""
        player = make_player()
        player.loop_mode = LoopMode.TRACK
        player.current = track("a")
        player.queue.put(track("b"))
        player._skip_requested = True
        self.assertEqual((await player._advance()).title, "b")

    async def test_loop_queue_sends_the_track_to_the_back(self):
        player = make_player()
        player.loop_mode = LoopMode.QUEUE
        player.current = track("a")
        player.queue.put(track("b"))
        self.assertEqual((await player._advance()).title, "b")
        self.assertEqual([item.title for item in player.queue], ["a"])

    async def test_loop_queue_still_rotates_on_skip(self):
        player = make_player()
        player.loop_mode = LoopMode.QUEUE
        player.current = track("a")
        player.queue.put(track("b"))
        player._skip_requested = True
        self.assertEqual((await player._advance()).title, "b")
        self.assertEqual([item.title for item in player.queue], ["a"])

    async def test_skip_flag_is_consumed(self):
        player = make_player()
        player.loop_mode = LoopMode.TRACK
        player.current = track("a")
        player.queue.put(track("b"))
        player._skip_requested = True
        await player._advance()
        self.assertFalse(player._skip_requested)

    async def test_a_failed_track_does_not_loop_forever(self):
        """After an extraction failure the loop clears `current`, so loop
        modes must not resurrect the broken track."""
        player = make_player()
        player.loop_mode = LoopMode.TRACK
        player.current = None
        self.assertIsNone(await player._advance())


class TestPlayPrevious(unittest.IsolatedAsyncioTestCase):
    async def test_requeues_the_last_track_ahead_of_the_current_one(self):
        player = make_player()
        player.history.appendleft(track("old"))
        player.current = track("now")
        returned = player.play_previous()
        self.assertEqual(returned.title, "old")
        self.assertEqual([item.title for item in player.queue], ["old", "now"])

    async def test_does_nothing_with_empty_history(self):
        player = make_player()
        self.assertIsNone(player.play_previous())

    async def test_going_back_does_not_readd_to_history(self):
        """Without the suppression flag, `back` twice would replay one track."""
        player = make_player()
        player.history.appendleft(track("old"))
        player.current = track("now")
        player.play_previous()
        await player._advance()
        self.assertEqual([item.title for item in player.history], [])


class TestPlaybackClock(unittest.TestCase):
    def test_position_is_zero_before_playback(self):
        self.assertEqual(make_player().position, 0.0)

    def test_position_counts_from_the_seek_offset(self):
        player = make_player()
        player._mark_started(30.0)
        self.assertGreaterEqual(player.position, 30.0)
        self.assertLess(player.position, 31.0)

    def test_paused_time_does_not_advance_the_position(self):
        import time

        player = make_player()
        player._mark_started(0.0)
        player._paused_at = time.monotonic()
        first = player.position
        time.sleep(0.05)
        self.assertAlmostEqual(first, player.position, places=3)

    def test_resuming_excludes_the_paused_interval(self):
        player = make_player()
        player._mark_started(10.0)
        # Simulate: played 5s, paused for 100s, resumed.
        player._play_started -= 5
        player._paused_total = 100.0
        player._play_started -= 100
        self.assertAlmostEqual(player.position, 15.0, places=1)

    def test_volume_is_clamped_to_the_supported_range(self):
        player = make_player()
        player.set_volume(500)
        self.assertEqual(player.volume_percent, 200)
        player.set_volume(-10)
        self.assertEqual(player.volume_percent, 0)


if __name__ == "__main__":
    unittest.main()
