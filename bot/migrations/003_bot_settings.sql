-- Bot-wide settings, as opposed to the per-guild ones in guild_config. The
-- owner-only `status` command writes here so a presence set from Discord
-- survives a restart instead of snapping back to ACTIVITY_NAME in the
-- environment. Keys are whitelisted in db.py, so a key/value table is enough.
CREATE TABLE IF NOT EXISTS bot_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
