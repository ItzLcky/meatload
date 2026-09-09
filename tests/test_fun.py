"""InspiroBot fetching.

The API hands back a bare URL as plain text, so `_fetch_poster` is the piece
worth testing: it has to reject anything that is not an InspiroBot link and
turn network failures into messages a user can read.
"""

import asyncio
import types
import unittest

import aiohttp

from bot.cogs.fun import Fun
from bot.utils.errors import FriendlyError


class FakeResponse:
    def __init__(self, body: str = "", error: Exception | None = None) -> None:
        self.body = body
        self.error = error

    def raise_for_status(self) -> None:
        if self.error is not None:
            raise self.error

    async def text(self) -> str:
        return self.body

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


async def make_cog(session) -> Fun:
    cog = Fun(types.SimpleNamespace())
    await cog.session.close()  # swap the real session out for the fake
    cog.session = session
    return cog


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


if __name__ == "__main__":
    unittest.main()
