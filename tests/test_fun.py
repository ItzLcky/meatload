"""The two Fun commands that talk to somebody else's server.

`_fetch_poster` and `_search_gif` both embed a URL that arrived over the wire,
so both have to reject links that are not the host they asked, and both have to
turn network failures into messages a user can read.
"""

import asyncio
import types
import unittest

import aiohttp

from bot.cogs.fun import Fun
from bot.utils.errors import FriendlyError


class FakeResponse:
    def __init__(
        self,
        body: str = "",
        error: Exception | None = None,
        payload: object = None,
        status: int = 200,
    ) -> None:
        self.body = body
        self.error = error
        self.payload = payload
        self.status = status

    def raise_for_status(self) -> None:
        if self.error is not None:
            raise self.error

    async def text(self) -> str:
        return self.body

    async def json(self, **_) -> object:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_) -> None:
        return None


class FakeSession:
    """Stands in for aiohttp: `get` either yields a response or raises."""

    def __init__(self, response: FakeResponse | None = None, raises: Exception | None = None) -> None:
        self.response = response
        self.raises = raises

    def get(self, url: str, **_):
        if self.raises is not None:
            raise self.raises
        return self.response

    async def close(self) -> None:
        return None


async def make_cog(session, tenor_api_key: str = "test-key") -> Fun:
    bot = types.SimpleNamespace(config=types.SimpleNamespace(tenor_api_key=tenor_api_key))
    cog = Fun(bot)
    await cog.session.close()  # swap the real session out for the fake
    cog.session = session
    return cog


def tenor_payload(*urls: str, key: str = "gif") -> dict:
    return {"results": [{"media_formats": {key: {"url": url}}} for url in urls]}


class TestFetchPoster(unittest.IsolatedAsyncioTestCase):
    async def test_returns_the_generated_image_url(self):
        url = "https://generated.inspirobot.me/a/qjep4Rg7en.jpg"
        cog = await make_cog(FakeSession(FakeResponse(f"{url}\n")))
        self.assertEqual(await cog._fetch_poster(), url)

    async def test_rejects_a_link_to_another_host(self):
        cog = await make_cog(FakeSession(FakeResponse("https://evil.example.com/a.jpg")))
        with self.assertRaises(FriendlyError):
            await cog._fetch_poster()

    async def test_a_lookalike_host_is_not_inspirobot(self):
        cog = await make_cog(FakeSession(FakeResponse("https://inspirobot.me.evil.example/a.jpg")))
        with self.assertRaises(FriendlyError):
            await cog._fetch_poster()

    async def test_rejects_a_body_that_is_not_a_url(self):
        cog = await make_cog(FakeSession(FakeResponse("service unavailable")))
        with self.assertRaises(FriendlyError):
            await cog._fetch_poster()

    async def test_an_http_error_becomes_a_friendly_error(self):
        response = FakeResponse(error=aiohttp.ClientResponseError(None, (), status=503))
        cog = await make_cog(FakeSession(response))
        with self.assertRaises(FriendlyError):
            await cog._fetch_poster()

    async def test_a_timeout_becomes_a_friendly_error(self):
        cog = await make_cog(FakeSession(raises=asyncio.TimeoutError()))
        with self.assertRaises(FriendlyError):
            await cog._fetch_poster()

    async def test_a_connection_failure_becomes_a_friendly_error(self):
        cog = await make_cog(FakeSession(raises=aiohttp.ClientConnectionError()))
        with self.assertRaises(FriendlyError):
            await cog._fetch_poster()


class TestSearchGif(unittest.IsolatedAsyncioTestCase):
    async def test_returns_one_of_the_results(self):
        urls = [
            "https://media.tenor.com/abc/cat.gif",
            "https://media1.tenor.com/def/cat.gif",
        ]
        cog = await make_cog(FakeSession(FakeResponse(payload=tenor_payload(*urls))))
        self.assertIn(await cog._search_gif("cat"), urls)

    async def test_picks_randomly_across_calls(self):
        """A fixed pick would make `gifr cat` post the same GIF every time."""
        urls = [f"https://media.tenor.com/{index}/cat.gif" for index in range(10)]
        cog = await make_cog(FakeSession(FakeResponse(payload=tenor_payload(*urls))))
        seen = {await cog._search_gif("cat") for _ in range(40)}
        self.assertGreater(len(seen), 1)

    async def test_sends_the_keyword_and_a_content_filter(self):
        captured = {}

        class RecordingSession(FakeSession):
            def get(self, url, **kwargs):
                captured["url"] = url
                captured["params"] = kwargs.get("params", {})
                return super().get(url, **kwargs)

        cog = await make_cog(
            RecordingSession(FakeResponse(payload=tenor_payload("https://media.tenor.com/a/b.gif")))
        )
        await cog._search_gif("happy dance")
        self.assertEqual(captured["params"]["q"], "happy dance")
        self.assertEqual(captured["params"]["key"], "test-key")
        self.assertEqual(captured["params"]["contentfilter"], "high")

    async def test_falls_back_to_a_smaller_format(self):
        payload = tenor_payload("https://media.tenor.com/a/small.gif", key="tinygif")
        cog = await make_cog(FakeSession(FakeResponse(payload=payload)))
        self.assertEqual(await cog._search_gif("cat"), "https://media.tenor.com/a/small.gif")

    async def test_skips_results_pointing_at_another_host(self):
        payload = tenor_payload(
            "https://evil.example.com/a.gif",
            "https://media.tenor.com/ok/cat.gif",
        )
        cog = await make_cog(FakeSession(FakeResponse(payload=payload)))
        self.assertEqual(await cog._search_gif("cat"), "https://media.tenor.com/ok/cat.gif")

    async def test_a_lookalike_host_is_not_tenor(self):
        payload = tenor_payload("https://tenor.com.evil.example/a.gif")
        cog = await make_cog(FakeSession(FakeResponse(payload=payload)))
        with self.assertRaises(FriendlyError):
            await cog._search_gif("cat")

    async def test_a_malformed_result_is_skipped_not_fatal(self):
        payload = {
            "results": [
                "not a dict",
                {"media_formats": None},
                {"media_formats": {"gif": {}}},
                {"media_formats": {"gif": {"url": "https://media.tenor.com/ok/cat.gif"}}},
            ]
        }
        cog = await make_cog(FakeSession(FakeResponse(payload=payload)))
        self.assertEqual(await cog._search_gif("cat"), "https://media.tenor.com/ok/cat.gif")

    async def test_no_results_becomes_a_friendly_error(self):
        cog = await make_cog(FakeSession(FakeResponse(payload={"results": []})))
        with self.assertRaises(FriendlyError):
            await cog._search_gif("asdkjhasd")

    async def test_a_missing_api_key_explains_the_setup(self):
        cog = await make_cog(FakeSession(), tenor_api_key="")
        with self.assertRaises(FriendlyError) as caught:
            await cog._search_gif("cat")
        self.assertIn("TENOR_API_KEY", str(caught.exception))

    async def test_a_rejected_key_becomes_a_friendly_error(self):
        cog = await make_cog(FakeSession(FakeResponse(status=403)))
        with self.assertRaises(FriendlyError):
            await cog._search_gif("cat")

    async def test_an_http_error_becomes_a_friendly_error(self):
        response = FakeResponse(error=aiohttp.ClientResponseError(None, (), status=503))
        cog = await make_cog(FakeSession(response))
        with self.assertRaises(FriendlyError):
            await cog._search_gif("cat")

    async def test_a_body_that_is_not_json_becomes_a_friendly_error(self):
        cog = await make_cog(FakeSession(FakeResponse(payload=ValueError("no json"))))
        with self.assertRaises(FriendlyError):
            await cog._search_gif("cat")

    async def test_a_timeout_becomes_a_friendly_error(self):
        cog = await make_cog(FakeSession(raises=asyncio.TimeoutError()))
        with self.assertRaises(FriendlyError):
            await cog._search_gif("cat")

    async def test_a_connection_failure_becomes_a_friendly_error(self):
        cog = await make_cog(FakeSession(raises=aiohttp.ClientConnectionError()))
        with self.assertRaises(FriendlyError):
            await cog._search_gif("cat")


if __name__ == "__main__":
    unittest.main()
