"""Placeholder substitution in tags and welcome messages."""

import types
import unittest

from bot.cogs.tags import (
    NAME_RE,
    pick_all_responses,
    pick_response,
    render as render_tag,
    split_red_create_syntax,
)
from bot.utils.errors import FriendlyError
from bot.cogs.welcome import render as render_welcome


class FakeAsset:
    url = "https://cdn.example/avatar.png"


class FakeAuthor:
    mention = "<@42>"
    display_name = "Sean"
    name = "seandobens"
    id = 42
    display_avatar = FakeAsset()

    def __str__(self) -> str:
        return "seandobens"


class FakeGuild:
    name = "Test Server"
    id = 999
    member_count = 101
    icon = FakeAsset()

    def __str__(self) -> str:
        return self.name


class FakeChannel:
    mention = "<#7>"
    name = "general"
    id = 7


def fake_context() -> types.SimpleNamespace:
    return types.SimpleNamespace(guild=FakeGuild(), author=FakeAuthor(), channel=FakeChannel())


class FakeMember(FakeAuthor):
    guild = FakeGuild()


class TestTagRendering(unittest.TestCase):
    def test_substitutes_the_original_placeholders(self):
        ctx = fake_context()
        out = render_tag("{user} {user.name} {user.id} {server} {channel} {count} {args}", ctx, 9, "hi there")
        self.assertEqual(out, "<@42> Sean 42 Test Server <#7> 9 hi there")

    def test_substitutes_the_placeholders_added_for_red(self):
        ctx = fake_context()
        out = render_tag(
            "{user.username} {user.tag} {user.avatar} {server.id} {server.members} "
            "{channel.name} {channel.id}",
            ctx,
            1,
            "",
        )
        self.assertEqual(
            out, "seandobens seandobens https://cdn.example/avatar.png 999 101 general 7"
        )

    def test_channel_mention_and_name_are_distinct(self):
        """Red's {channel} is the name; ours is a mention. Both must be reachable."""
        ctx = fake_context()
        self.assertEqual(render_tag("{channel}", ctx, 1, ""), "<#7>")
        self.assertEqual(render_tag("{channel.name}", ctx, 1, ""), "general")

    def test_positional_arguments(self):
        ctx = fake_context()
        self.assertEqual(render_tag("{0} and {1}", ctx, 1, "alpha beta"), "alpha and beta")

    def test_missing_positional_arguments_render_empty(self):
        ctx = fake_context()
        self.assertEqual(render_tag("[{0}][{1}][{5}]", ctx, 1, "only"), "[only][][]")

    def test_positional_and_args_coexist(self):
        ctx = fake_context()
        self.assertEqual(render_tag("{0} | {args}", ctx, 1, "a b c"), "a | a b c")

    def test_leaves_unknown_braces_alone(self):
        """Tags are not str.format, so `{0.__class__}` is inert text."""
        ctx = fake_context()
        self.assertEqual(render_tag("{0.__class__} {nope}", ctx, 1, ""), "{0.__class__} {nope}")

    def test_empty_args_substitutes_to_nothing(self):
        self.assertEqual(render_tag("say: {args}", fake_context(), 1, ""), "say: ")

    def test_longer_placeholders_are_not_clipped_by_shorter_ones(self):
        """`{user}` must not eat the prefix of `{user.name}`."""
        ctx = fake_context()
        self.assertEqual(render_tag("{user.name}", ctx, 1, ""), "Sean")
        self.assertEqual(render_tag("{server.id}", ctx, 1, ""), "999")


class TestResponseSelection(unittest.TestCase):
    def test_single_response(self):
        self.assertEqual(pick_response({"content": "hello", "is_random": 0}), "hello")

    def test_random_response_comes_from_the_stored_list(self):
        row = {"content": '["a", "b", "c"]', "is_random": 1}
        for _ in range(25):
            self.assertIn(pick_response(row), {"a", "b", "c"})

    def test_malformed_random_content_falls_back_to_raw_text(self):
        """A hand-edited row must not take the tag out of service."""
        row = {"content": "not json", "is_random": 1}
        self.assertEqual(pick_response(row), "not json")

    def test_export_lists_every_response(self):
        self.assertEqual(pick_all_responses({"content": '["a","b"]', "is_random": 1}), ["a", "b"])
        self.assertEqual(pick_all_responses({"content": "solo", "is_random": 0}), ["solo"])


class TestTagNames(unittest.TestCase):
    def test_accepts_reasonable_names(self):
        for name in ("hi", "my-tag", "tag_1", "a" * 32):
            with self.subTest(name=name):
                self.assertIsNotNone(NAME_RE.match(name))

    def test_rejects_bad_names(self):
        for name in ("", "has space", "UPPER", "a" * 33, "emoji😀", "semi;colon"):
            with self.subTest(name=name):
                self.assertIsNone(NAME_RE.match(name))


class TestRedCreateSyntax(unittest.TestCase):
    """`cc add simple <name> <text>` is muscle memory for anyone leaving Red.

    The cog only applies this under the `cc`/`customcom` spelling of the group,
    so `!tag create simple <anything>` still makes a tag called `simple`.
    """

    def test_simple_keyword_is_unwrapped(self):
        self.assertEqual(
            split_red_create_syntax("simple", "greet Hi there"), ("simple", "greet", "Hi there")
        )

    def test_random_keyword_is_unwrapped(self):
        self.assertEqual(
            split_red_create_syntax("random", "greet Hi!|Hello!"), ("random", "greet", "Hi!|Hello!")
        )

    def test_keyword_matching_ignores_case_and_spacing(self):
        self.assertEqual(
            split_red_create_syntax(" Simple ", "  greet   Hi there  "),
            ("simple", "greet", "Hi there"),
        )

    def test_ordinary_creation_is_untouched(self):
        self.assertEqual(split_red_create_syntax("greet", "Hi there"), (None, "greet", "Hi there"))

    def test_content_that_cannot_be_a_name_is_left_alone(self):
        """No name follows the keyword, so it was the tag's own name."""
        self.assertEqual(
            split_red_create_syntax("simple", "it's that easy"), (None, "simple", "it's that easy")
        )
        self.assertEqual(
            split_red_create_syntax("random", "🎲 rolls a die"),
            (None, "random", "🎲 rolls a die"),
        )

    def test_bare_red_form_asks_instead_of_guessing(self):
        """`!cc add random greet` reads two ways, so neither is assumed."""
        with self.assertRaises(FriendlyError):
            split_red_create_syntax("random", "greet")

    def test_the_error_echoes_the_prefix_that_was_used(self):
        with self.assertRaises(FriendlyError) as caught:
            split_red_create_syntax("simple", "greet", "?cc ")
        self.assertIn("?cc add greet", str(caught.exception))


class TestWelcomeRendering(unittest.TestCase):
    def test_substitutes_placeholders(self):
        out = render_welcome(
            "{user} joined {server} as member #{count} ({user.name}/{user.id})", FakeMember()
        )
        self.assertEqual(out, "<@42> joined Test Server as member #101 (Sean/42)")

    def test_user_tag(self):
        self.assertEqual(render_welcome("{user.tag}", FakeMember()), "seandobens")


if __name__ == "__main__":
    unittest.main()
