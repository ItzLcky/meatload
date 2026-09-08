-- Red-DiscordBot supports custom commands with several responses, one chosen at
-- random per invocation. Representing that faithfully needs a single flag: when
-- is_random is set, `content` holds a JSON array of responses rather than plain
-- text. Existing tags default to 0 and are unaffected.
ALTER TABLE tags ADD COLUMN is_random INTEGER NOT NULL DEFAULT 0;
