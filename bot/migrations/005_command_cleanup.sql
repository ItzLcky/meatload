-- Optional tidying of command invocations. With this on, the bot deletes the
-- message that ran a text command once the command has answered, so a busy
-- channel keeps the replies instead of both halves of every exchange.
--
-- Off by default: deleting members' messages is a surprise for a server that
-- didn't ask for it, and it needs Manage Messages to work at all.
ALTER TABLE guild_config ADD COLUMN delete_command_messages INTEGER NOT NULL DEFAULT 0;
