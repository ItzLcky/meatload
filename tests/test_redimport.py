"""Reading Red-DiscordBot custom commands and translating them."""

import json
import unittest

from bot import redimport

GUILD = "133049272517001216"
OTHER_GUILD = "222222222222222222"


def red3(commands: dict, guild: str = GUILD) -> dict:
    """The shape Red 3's JSON driver writes."""
    return {redimport.RED_CUSTOMCOM_IDENTIFIER: {"GUILD": {guild: {"commands": commands}}}}


def entry(response, **extra) -> dict:
    return {"response": response, "author": {"id": 99, "name": "someone"}, **extra}


class TestExtraction(unittest.TestCase):
    def test_reads_the_red3_layout(self):
        found = redimport.extract_guild_commands(red3({"hello": entry("hi there")}))
        self.assertEqual(list(found), [int(GUILD)])
        self.assertIn("hello", found[int(GUILD)])

    def test_reads_the_old_red2_flat_layout(self):
        found = redimport.extract_guild_commands({GUILD: {"hello": "hi there"}})
        self.assertEqual(found[int(GUILD)]["hello"]["response"], "hi there")

    def test_cog_identifier_is_not_mistaken_for_a_guild(self):
        """Red's identifier is 15 digits; guild snowflakes are 17+."""
        found = redimport.extract_guild_commands(red3({"hello": entry("hi")}))
        self.assertNotIn(int(redimport.RED_CUSTOMCOM_IDENTIFIER), found)

    def test_finds_commands_at_unexpected_depths(self):
        """A hand-trimmed or differently-nested file should still work."""
        nested = {"anything": {"nested": {"GUILD": {GUILD: {"commands": {"a": entry("b")}}}}}}
        self.assertIn(int(GUILD), redimport.extract_guild_commands(nested))

    def test_separates_multiple_guilds(self):
        data = {
            redimport.RED_CUSTOMCOM_IDENTIFIER: {
                "GUILD": {
                    GUILD: {"commands": {"a": entry("1")}},
                    OTHER_GUILD: {"commands": {"b": entry("2")}},
                }
            }
        }
        found = redimport.extract_guild_commands(data)
        self.assertEqual(sorted(found), sorted([int(GUILD), int(OTHER_GUILD)]))

    def test_empty_document_finds_nothing(self):
        self.assertEqual(redimport.extract_guild_commands({}), {})
        self.assertFalse(redimport.looks_like_red_data({"unrelated": True}))


class TestPlaceholderTranslation(unittest.TestCase):
    def test_author_forms(self):
        cases = {
            "{author}": "{user.tag}",
            "{author.name}": "{user.username}",
            "{author.mention}": "{user}",
            "{author.display_name}": "{user.name}",
            "{author.id}": "{user.id}",
        }
        for red, ours in cases.items():
            with self.subTest(red=red):
                self.assertEqual(redimport.translate_response(red)[0], ours)

    def test_bare_channel_becomes_the_name_not_a_mention(self):
        """Red renders {channel} as str(channel), which is the name."""
        self.assertEqual(redimport.translate_response("{channel}")[0], "{channel.name}")
        self.assertEqual(redimport.translate_response("{channel.mention}")[0], "{channel}")

    def test_server_and_guild_are_interchangeable(self):
        self.assertEqual(redimport.translate_response("{server}")[0], "{server}")
        self.assertEqual(redimport.translate_response("{guild.name}")[0], "{server}")
        self.assertEqual(redimport.translate_response("{guild.id}")[0], "{server.id}")

    def test_positional_arguments_pass_through(self):
        text, unknown = redimport.translate_response("Hello {0}, meet {1}")
        self.assertEqual(text, "Hello {0}, meet {1}")
        self.assertFalse(unknown)

    def test_unknown_placeholders_are_reported_and_preserved(self):
        text, unknown = redimport.translate_response("weird {author.top_role} thing")
        self.assertEqual(text, "weird {author.top_role} thing")
        self.assertEqual(unknown, {"{author.top_role}"})

    def test_surrounding_text_is_untouched(self):
        text, _ = redimport.translate_response("Hi {author.mention}! Welcome to {server}.")
        self.assertEqual(text, "Hi {user}! Welcome to {server}.")

    def test_braces_that_are_not_placeholders_survive(self):
        text, _ = redimport.translate_response("json: {} and {  }")
        self.assertEqual(text, "json: {} and {  }")


class TestNameSanitising(unittest.TestCase):
    def test_leaves_good_names_alone(self):
        self.assertEqual(redimport.sanitise_name("hello"), "hello")
        self.assertEqual(redimport.sanitise_name("my-tag_2"), "my-tag_2")

    def test_lowercases_and_replaces_illegal_characters(self):
        self.assertEqual(redimport.sanitise_name("Hello World"), "hello-world")
        self.assertEqual(redimport.sanitise_name("what?!"), "what")

    def test_truncates_to_the_column_limit(self):
        self.assertEqual(len(redimport.sanitise_name("x" * 80)), 32)

    def test_unusable_names_return_none(self):
        for name in ("", "   ", "!!!", "😀"):
            with self.subTest(name=name):
                self.assertIsNone(redimport.sanitise_name(name))

    def test_sanitised_names_are_always_valid_tags(self):
        for name in ("Hello World", "UPPER", "a b c", "tag!!!", "x" * 80):
            with self.subTest(name=name):
                self.assertIsNotNone(redimport.NAME_RE.match(redimport.sanitise_name(name)))


class TestBuildReport(unittest.TestCase):
    def test_simple_import(self):
        report = redimport.build_report(red3({"hello": entry("hi {author.mention}")}))
        self.assertEqual(report.guild_id, int(GUILD))
        command = report.commands[0]
        self.assertEqual(command.name, "hello")
        self.assertEqual(command.responses, ["hi {user}"])
        self.assertFalse(command.is_random)
        self.assertEqual(command.content, "hi {user}")
        self.assertEqual(command.author_id, 99)

    def test_multi_response_becomes_a_random_tag(self):
        report = redimport.build_report(red3({"greet": entry(["hi", "hello", "yo"])}))
        command = report.commands[0]
        self.assertTrue(command.is_random)
        self.assertEqual(json.loads(command.content), ["hi", "hello", "yo"])
        self.assertIn("3 random responses", " ".join(command.notes))

    def test_single_item_list_is_not_random(self):
        command = redimport.build_report(red3({"a": entry(["only"])})).commands[0]
        self.assertFalse(command.is_random)
        self.assertEqual(command.content, "only")

    def test_empty_responses_are_skipped(self):
        report = redimport.build_report(red3({"a": entry(""), "b": entry("ok")}))
        self.assertEqual([c.name for c in report.commands], ["b"])
        self.assertEqual(report.skipped, [("a", "empty response")])

    def test_reserved_names_are_skipped_not_imported(self):
        """A tag named `play` could never fire, so importing it would be a lie."""
        report = redimport.build_report(
            red3({"play": entry("x"), "fine": entry("y")}), reserved_names={"play"}
        )
        self.assertEqual([c.name for c in report.commands], ["fine"])
        self.assertIn("built-in", report.skipped[0][1])

    def test_renaming_is_recorded(self):
        command = redimport.build_report(red3({"Hello World": entry("x")})).commands[0]
        self.assertEqual(command.name, "hello-world")
        self.assertTrue(command.renamed)
        self.assertIn("renamed", " ".join(command.notes))

    def test_collision_after_renaming_is_skipped(self):
        report = redimport.build_report(red3({"hi there": entry("a"), "hi-there": entry("b")}))
        self.assertEqual(len(report.commands), 1)
        self.assertIn("collides", report.skipped[0][1])

    def test_cooldowns_are_flagged_as_dropped(self):
        command = redimport.build_report(
            red3({"a": entry("x", cooldowns={"member": 30})})
        ).commands[0]
        self.assertTrue(command.had_cooldowns)
        self.assertIn("cooldown", " ".join(command.notes))

    def test_multiple_guilds_requires_choosing_one(self):
        data = {
            redimport.RED_CUSTOMCOM_IDENTIFIER: {
                "GUILD": {
                    GUILD: {"commands": {"a": entry("1")}},
                    OTHER_GUILD: {"commands": {"b": entry("2")}},
                }
            }
        }
        with self.assertRaises(redimport.RedImportError) as caught:
            redimport.build_report(data)
        self.assertIn(GUILD, str(caught.exception))

        report = redimport.build_report(data, int(OTHER_GUILD))
        self.assertEqual([c.name for c in report.commands], ["b"])

    def test_unknown_guild_is_an_error_that_lists_the_options(self):
        with self.assertRaises(redimport.RedImportError) as caught:
            redimport.build_report(red3({"a": entry("1")}), 999999999999999999)
        self.assertIn(GUILD, str(caught.exception))

    def test_unrecognisable_file_is_rejected(self):
        with self.assertRaises(redimport.RedImportError):
            redimport.build_report({"nothing": "here"})

    def test_bad_json_gives_a_readable_error(self):
        with self.assertRaises(redimport.RedImportError) as caught:
            redimport.load_json("{not json")
        self.assertIn("valid JSON", str(caught.exception))


class TestTimestampsAndAuthors(unittest.TestCase):
    def test_iso_created_at(self):
        command = redimport.build_report(
            red3({"a": entry("x", created_at="2020-05-01T12:00:00+00:00")})
        ).commands[0]
        self.assertIsNotNone(command.created_at)

    def test_epoch_created_at(self):
        command = redimport.build_report(red3({"a": entry("x", created_at=1600000000)})).commands[0]
        self.assertEqual(command.created_at, 1600000000.0)

    def test_garbage_created_at_is_ignored(self):
        command = redimport.build_report(red3({"a": entry("x", created_at="whenever")})).commands[0]
        self.assertIsNone(command.created_at)

    def test_author_as_a_bare_id(self):
        command = redimport.build_report(red3({"a": {"response": "x", "author": 55}})).commands[0]
        self.assertEqual(command.author_id, 55)

    def test_missing_author(self):
        command = redimport.build_report(red3({"a": {"response": "x"}})).commands[0]
        self.assertIsNone(command.author_id)


class TestNativeFormat(unittest.TestCase):
    def payload(self) -> dict:
        return {
            "format": redimport.NATIVE_FORMAT,
            "guild_id": GUILD,
            "tags": [
                {"name": "hello", "responses": ["hi"], "owner_id": 7, "aliases": ["hey", "yo"]},
                {"name": "greet", "responses": ["a", "b"], "owner_id": 7},
            ],
        }

    def test_round_trips_an_export(self):
        report, label = redimport.build_any_report(self.payload())
        self.assertEqual(label, "backup")
        self.assertEqual(report.guild_id, int(GUILD))
        self.assertEqual([c.name for c in report.commands], ["hello", "greet"])

    def test_aliases_survive(self):
        report, _ = redimport.build_any_report(self.payload())
        self.assertEqual(report.commands[0].aliases, ["hey", "yo"])

    def test_random_tags_survive(self):
        report, _ = redimport.build_any_report(self.payload())
        self.assertTrue(report.commands[1].is_random)

    def test_red_files_still_route_to_the_red_parser(self):
        _, label = redimport.build_any_report(red3({"a": entry("x")}))
        self.assertEqual(label, "Red")


if __name__ == "__main__":
    unittest.main()
