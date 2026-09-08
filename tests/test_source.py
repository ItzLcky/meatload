import unittest

from bot.music.source import (
    STREAM_TTL_SECONDS,
    Track,
    _friendly_error,
    _is_playlist_url,
    _parse_extractor_args,
    _pick_thumbnail,
)


class TestPlaylistDetection(unittest.TestCase):
    def test_youtube_playlist_links_expand(self):
        self.assertTrue(_is_playlist_url("https://www.youtube.com/playlist?list=PL123"))

    def test_a_video_opened_from_a_playlist_does_not_expand(self):
        """`watch?v=X&list=Y` means "this video", not "these 200 videos"."""
        self.assertFalse(_is_playlist_url("https://www.youtube.com/watch?v=abc&list=PL123"))

    def test_plain_video_links_do_not_expand(self):
        self.assertFalse(_is_playlist_url("https://youtu.be/dQw4w9WgXcQ"))

    def test_soundcloud_and_bandcamp_sets(self):
        self.assertTrue(_is_playlist_url("https://soundcloud.com/artist/sets/my-set"))
        self.assertTrue(_is_playlist_url("https://artist.bandcamp.com/album/thing"))


class TestExtractorArgs(unittest.TestCase):
    def test_single_extractor(self):
        self.assertEqual(
            _parse_extractor_args("youtube:player_client=web_safari,default"),
            {"youtube": {"player_client": ["web_safari", "default"]}},
        )

    def test_multiple_extractors(self):
        self.assertEqual(
            _parse_extractor_args("youtube:player_client=android; soundcloud:key=value"),
            {"youtube": {"player_client": ["android"]}, "soundcloud": {"key": ["value"]}},
        )

    def test_garbage_is_ignored_rather_than_raising(self):
        self.assertEqual(_parse_extractor_args(""), {})
        self.assertEqual(_parse_extractor_args("no-colon-here"), {})


class TestFriendlyError(unittest.TestCase):
    def test_bot_check_points_at_cookies(self):
        message = _friendly_error("ERROR: [youtube] abc: Sign in to confirm you're not a bot")
        self.assertIn("cookies", message.lower())

    def test_private_video(self):
        self.assertIn("private", _friendly_error("ERROR: Private video").lower())

    def test_strips_the_ytdlp_prefix(self):
        self.assertNotIn("ERROR:", _friendly_error("ERROR: [youtube] xyz: something odd happened"))


class TestTrack(unittest.TestCase):
    def test_needs_resolution_when_unresolved(self):
        self.assertTrue(Track(title="t", url="u", requester_id=1).needs_resolution)

    def test_fresh_stream_url_is_reused(self):
        import time

        track = Track(title="t", url="u", requester_id=1)
        track.stream_url = "https://media"
        track._resolved_at = time.time()
        self.assertFalse(track.needs_resolution)

    def test_expired_stream_url_is_refetched(self):
        import time

        track = Track(title="t", url="u", requester_id=1)
        track.stream_url = "https://media"
        track._resolved_at = time.time() - STREAM_TTL_SECONDS - 1
        self.assertTrue(track.needs_resolution)

    def test_display_links_real_urls_only(self):
        linked = Track(title="Song", url="https://youtu.be/x", requester_id=1)
        self.assertEqual(linked.display(), "[Song](https://youtu.be/x)")
        # Spotify tracks can arrive with no URL at all.
        bare = Track(title="Song", url="", requester_id=1)
        self.assertEqual(bare.display(), "Song")

    def test_display_truncates_long_titles(self):
        long_title = Track(title="x" * 200, url="", requester_id=1)
        self.assertEqual(len(long_title.display(30)), 30)


class TestPickThumbnail(unittest.TestCase):
    def test_prefers_the_explicit_field(self):
        self.assertEqual(_pick_thumbnail({"thumbnail": "a", "thumbnails": [{"url": "b"}]}), "a")

    def test_falls_back_to_the_largest_listed(self):
        self.assertEqual(_pick_thumbnail({"thumbnails": [{"url": "small"}, {"url": "big"}]}), "big")

    def test_none_when_absent(self):
        self.assertIsNone(_pick_thumbnail({}))


if __name__ == "__main__":
    unittest.main()
