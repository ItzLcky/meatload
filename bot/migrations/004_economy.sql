-- Culture Coin (cc) and Culshi, its prediction market.
--
-- Money is stored in whole cc, never floats: every amount in this schema is an
-- INTEGER, and the market maker's fractional prices are rounded at the edge (in
-- bot/economy/lmsr.py) rather than accumulating rounding error in the ledger.

-- Economy settings live alongside the rest of the per-guild configuration.
ALTER TABLE guild_config ADD COLUMN economy_enabled       INTEGER NOT NULL DEFAULT 1;
ALTER TABLE guild_config ADD COLUMN cc_start_balance      INTEGER NOT NULL DEFAULT 500;
ALTER TABLE guild_config ADD COLUMN cc_chat_min           INTEGER NOT NULL DEFAULT 2;
ALTER TABLE guild_config ADD COLUMN cc_chat_max           INTEGER NOT NULL DEFAULT 6;
ALTER TABLE guild_config ADD COLUMN cc_chat_cooldown      INTEGER NOT NULL DEFAULT 60;
ALTER TABLE guild_config ADD COLUMN cc_daily_amount       INTEGER NOT NULL DEFAULT 250;
ALTER TABLE guild_config ADD COLUMN cc_daily_streak_bonus INTEGER NOT NULL DEFAULT 25;
ALTER TABLE guild_config ADD COLUMN cc_work_min           INTEGER NOT NULL DEFAULT 60;
ALTER TABLE guild_config ADD COLUMN cc_work_max           INTEGER NOT NULL DEFAULT 220;
ALTER TABLE guild_config ADD COLUMN cc_work_cooldown      INTEGER NOT NULL DEFAULT 3600;
ALTER TABLE guild_config ADD COLUMN culshi_enabled        INTEGER NOT NULL DEFAULT 1;
ALTER TABLE guild_config ADD COLUMN culshi_channel_id     INTEGER;
ALTER TABLE guild_config ADD COLUMN culshi_steward_role_id INTEGER;
ALTER TABLE guild_config ADD COLUMN culshi_min_subsidy    INTEGER NOT NULL DEFAULT 100;
ALTER TABLE guild_config ADD COLUMN culshi_max_open       INTEGER NOT NULL DEFAULT 25;

-- One wallet per member per server. Balances are guild-scoped so one server's
-- inflation cannot leak into another's.
CREATE TABLE IF NOT EXISTS balances (
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    balance         INTEGER NOT NULL DEFAULT 0,
    lifetime_earned INTEGER NOT NULL DEFAULT 0,
    last_chat_at    REAL    NOT NULL DEFAULT 0,
    last_daily_at   REAL    NOT NULL DEFAULT 0,
    last_work_at    REAL    NOT NULL DEFAULT 0,
    daily_streak    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_balances_richest ON balances (guild_id, balance DESC);

-- Every movement of cc, append-only. `/balance history` reads it, and it is
-- what makes a wrong payout diagnosable after the fact.
CREATE TABLE IF NOT EXISTS cc_ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    delta      INTEGER NOT NULL,
    balance    INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    note       TEXT,
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cc_ledger_user ON cc_ledger (guild_id, user_id, id DESC);

-- Channels that pay no cc for chatting.
CREATE TABLE IF NOT EXISTS economy_ignores (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

-- A Culshi market: one binary question, priced by an LMSR market maker the
-- creator funds. `liquidity` is the LMSR b parameter; `q_yes`/`q_no` are the
-- contracts outstanding on each side and are all the state pricing needs.
-- `pot` is the escrow: the subsidy plus everything spent, minus everything
-- refunded, and it is what settlement pays out of.
CREATE TABLE IF NOT EXISTS culshi_markets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    creator_id  INTEGER NOT NULL,
    subject_id  INTEGER,
    question    TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'open',
    liquidity   REAL    NOT NULL,
    subsidy     INTEGER NOT NULL,
    pot         INTEGER NOT NULL,
    q_yes       INTEGER NOT NULL DEFAULT 0,
    q_no        INTEGER NOT NULL DEFAULT 0,
    volume      INTEGER NOT NULL DEFAULT 0,
    channel_id  INTEGER,
    created_at  REAL    NOT NULL,
    closes_at   REAL    NOT NULL,
    resolved_at REAL,
    outcome     TEXT,
    resolver_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_culshi_markets_guild ON culshi_markets (guild_id, status, closes_at);
CREATE INDEX IF NOT EXISTS idx_culshi_markets_subject ON culshi_markets (guild_id, subject_id);

-- What each trader holds. `spent` is their net cost basis: cc paid in for buys
-- minus cc taken back out on sells, so it can go negative once someone has
-- sold at a profit.
CREATE TABLE IF NOT EXISTS culshi_positions (
    market_id INTEGER NOT NULL REFERENCES culshi_markets (id) ON DELETE CASCADE,
    user_id   INTEGER NOT NULL,
    yes       INTEGER NOT NULL DEFAULT 0,
    no        INTEGER NOT NULL DEFAULT 0,
    spent     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (market_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_culshi_positions_user ON culshi_positions (user_id);

-- The tape. Kept for `/culshi view`'s recent-trades list and for auditing a
-- market whose numbers look wrong.
CREATE TABLE IF NOT EXISTS culshi_trades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id  INTEGER NOT NULL REFERENCES culshi_markets (id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL,
    side       TEXT    NOT NULL,
    action     TEXT    NOT NULL,
    contracts  INTEGER NOT NULL,
    cc         INTEGER NOT NULL,
    price      INTEGER NOT NULL,
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_culshi_trades_market ON culshi_trades (market_id, id DESC);
