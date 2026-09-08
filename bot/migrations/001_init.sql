-- Per-guild settings. A row is created lazily the first time a guild is
-- configured or looked up; every column is nullable so "unset" is meaningful.
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id            INTEGER PRIMARY KEY,
    prefix              TEXT,
    modlog_channel_id   INTEGER,
    welcome_channel_id  INTEGER,
    welcome_message     TEXT,
    leave_channel_id    INTEGER,
    leave_message       TEXT,
    autorole_id         INTEGER,
    dj_role_id          INTEGER,
    tag_manager_role_id INTEGER,
    music_volume        INTEGER,
    leveling_enabled    INTEGER NOT NULL DEFAULT 0,
    levelup_channel_id  INTEGER,
    levelup_message     TEXT,
    xp_min              INTEGER NOT NULL DEFAULT 15,
    xp_max              INTEGER NOT NULL DEFAULT 25,
    xp_cooldown         INTEGER NOT NULL DEFAULT 60
);

-- Custom commands.
CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    name       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    owner_id   INTEGER NOT NULL,
    uses       INTEGER NOT NULL DEFAULT 0,
    created_at REAL    NOT NULL,
    updated_at REAL    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_guild_name ON tags (guild_id, name);

CREATE TABLE IF NOT EXISTS tag_aliases (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    alias    TEXT    NOT NULL,
    tag_id   INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_aliases_guild_alias ON tag_aliases (guild_id, alias);

-- Moderation history. Warnings are just cases with action = 'warn', so
-- `/warnings` and the mod-log read from the same place.
CREATE TABLE IF NOT EXISTS mod_cases (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    case_number      INTEGER NOT NULL,
    action           TEXT    NOT NULL,
    target_id        INTEGER NOT NULL,
    moderator_id     INTEGER NOT NULL,
    reason           TEXT,
    duration_seconds INTEGER,
    created_at       REAL    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mod_cases_number ON mod_cases (guild_id, case_number);
CREATE INDEX IF NOT EXISTS idx_mod_cases_target ON mod_cases (guild_id, target_id);

-- Leveling. `level` is derived from `xp` in code, never stored, so the
-- curve can be changed without a migration.
CREATE TABLE IF NOT EXISTS levels (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    xp         INTEGER NOT NULL DEFAULT 0,
    messages   INTEGER NOT NULL DEFAULT 0,
    last_xp_at REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_levels_leaderboard ON levels (guild_id, xp DESC);

CREATE TABLE IF NOT EXISTS level_rewards (
    guild_id INTEGER NOT NULL,
    level    INTEGER NOT NULL,
    role_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, level)
);

CREATE TABLE IF NOT EXISTS level_ignores (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

-- Reminders survive restarts: the scheduler reloads pending rows on boot.
CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    guild_id   INTEGER,
    jump_url   TEXT,
    content    TEXT    NOT NULL,
    remind_at  REAL    NOT NULL,
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (remind_at);

-- Self-assign role panels (buttons, not reactions).
CREATE TABLE IF NOT EXISTS role_panels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    title       TEXT    NOT NULL,
    description TEXT,
    max_roles   INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_role_panels_guild ON role_panels (guild_id);

CREATE TABLE IF NOT EXISTS role_panel_items (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_id INTEGER NOT NULL REFERENCES role_panels (id) ON DELETE CASCADE,
    role_id  INTEGER NOT NULL,
    label    TEXT    NOT NULL,
    emoji    TEXT,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_role_panel_items_panel ON role_panel_items (panel_id);

-- Saved queues.
CREATE TABLE IF NOT EXISTS playlists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    owner_id   INTEGER NOT NULL,
    name       TEXT    NOT NULL,
    created_at REAL    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_playlists_guild_name ON playlists (guild_id, name);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists (id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    title       TEXT    NOT NULL,
    url         TEXT    NOT NULL,
    duration    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_playlist ON playlist_tracks (playlist_id, position);
