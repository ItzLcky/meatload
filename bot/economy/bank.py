"""Wallets and the cc ledger.

Every function here moves money through the smallest number of SQL statements
that can be made safe, because there is no transaction wrapping them: the bot
shares one autocommitting connection, so a multi-statement "transaction" here
could be committed halfway through by any other cog's write landing in between.

Two rules make that survivable:

* **A debit is a single conditional UPDATE.** `WHERE balance >= amount` plus a
  rowcount check is what stops two commands racing on the same wallet from both
  passing an "is there enough?" test and overdrawing it. Never read a balance,
  decide, and then write it back.
* **Debit before credit, always.** If the process dies mid-move the coins are
  gone rather than duplicated, and the ledger shows exactly where they stopped.
  Losing cc is a bug report; minting it is an exploit.

`spend`/`earn` are the whole vocabulary. The ledger row is written by the same
helpers, so an amount that moves without a ledger entry has to be a deliberate
detour rather than an oversight.
"""

from __future__ import annotations

import time
from typing import Any

CURRENCY = "cc"
CURRENCY_NAME = "Culture Coin"
COIN = "🪙"

# Ledger `kind` values. Free-text would work, but a closed set keeps
# `/balance history` readable and makes a typo in one cog visible in the other.
KIND_CHAT = "chat"
KIND_DAILY = "daily"
KIND_WORK = "work"
KIND_PAY_IN = "pay-in"
KIND_PAY_OUT = "pay-out"
KIND_ADMIN = "admin"
KIND_BUY = "culshi-buy"
KIND_SELL = "culshi-sell"
KIND_SUBSIDY = "culshi-subsidy"
KIND_SETTLE = "culshi-settle"
KIND_REFUND = "culshi-refund"

# `lifetime_earned` answers "how much cc has this person generated?", so it
# counts income only. Selling a position, being paid out, or getting a refund
# moves cc that was already yours, and counting it would make the number
# meaningless for anyone who trades. `/culshi pnl` is where trading is judged.
INCOME_KINDS = frozenset({KIND_CHAT, KIND_DAILY, KIND_WORK, KIND_ADMIN, KIND_PAY_IN})

DAILY_COOLDOWN = 20 * 3600  # claimable once a day without being clock-exact
STREAK_WINDOW = 48 * 3600  # miss two days and the streak is gone


def format_cc(amount: int, *, sign: bool = False) -> str:
    """`1,250 cc`, or `+1,250 cc` where the direction is the point."""
    if sign:
        return f"{amount:+,} {CURRENCY}"
    return f"{amount:,} {CURRENCY}"


def streak_after(last_claim_at: float, streak: int, now: float) -> int:
    """The streak a claim at `now` earns. Pure so the edges are testable."""
    if last_claim_at <= 0 or now - last_claim_at > STREAK_WINDOW:
        return 1
    return streak + 1


async def ensure_account(db, guild_id: int, user_id: int, start_balance: int = 0) -> None:
    """Open a wallet if this member has never had one, seeded with the guild's
    starting balance. `INSERT OR IGNORE` so concurrent callers cannot double-seed."""
    cursor = await db.execute(
        "INSERT OR IGNORE INTO balances (guild_id, user_id, balance, lifetime_earned)"
        " VALUES (?, ?, ?, ?)",
        (guild_id, user_id, start_balance, start_balance),
    )
    if cursor.rowcount and start_balance:
        await _ledger(db, guild_id, user_id, start_balance, start_balance, KIND_ADMIN, "Opening balance")


async def account(db, guild_id: int, user_id: int, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """A member's wallet row, creating it on first look."""
    start = int((config or {}).get("cc_start_balance") or 0)
    await ensure_account(db, guild_id, user_id, start)
    row = await db.fetchone(
        "SELECT * FROM balances WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
    )
    return dict(row) if row is not None else {"guild_id": guild_id, "user_id": user_id, "balance": 0}


async def balance(db, guild_id: int, user_id: int) -> int:
    return int(await db.fetchval(
        "SELECT balance FROM balances WHERE guild_id = ? AND user_id = ?", (guild_id, user_id), default=0
    ) or 0)


async def earn(db, guild_id: int, user_id: int, amount: int, kind: str, note: str | None = None) -> int:
    """Credit a wallet. Returns the new balance.

    Whether this counts towards `lifetime_earned` is decided by `kind` rather
    than by the caller, so the two cogs cannot disagree about what income means.
    """
    if amount <= 0:
        return await balance(db, guild_id, user_id)
    await ensure_account(db, guild_id, user_id)
    income = amount if kind in INCOME_KINDS else 0
    await db.execute(
        "UPDATE balances SET balance = balance + ?, lifetime_earned = lifetime_earned + ?"
        " WHERE guild_id = ? AND user_id = ?",
        (amount, income, guild_id, user_id),
    )
    new_balance = await balance(db, guild_id, user_id)
    await _ledger(db, guild_id, user_id, amount, new_balance, kind, note)
    return new_balance


async def spend(db, guild_id: int, user_id: int, amount: int, kind: str, note: str | None = None) -> bool:
    """Debit a wallet, but only if it covers the amount. False means it didn't.

    The `balance >= ?` guard is the entire overdraft defence — it makes the
    check and the deduction one statement, so two simultaneous trades cannot
    both spend the same coins.
    """
    if amount <= 0:
        return True
    await ensure_account(db, guild_id, user_id)
    cursor = await db.execute(
        "UPDATE balances SET balance = balance - ? WHERE guild_id = ? AND user_id = ? AND balance >= ?",
        (amount, guild_id, user_id, amount),
    )
    if cursor.rowcount != 1:
        return False
    new_balance = await balance(db, guild_id, user_id)
    await _ledger(db, guild_id, user_id, -amount, new_balance, kind, note)
    return True


async def force_set(db, guild_id: int, user_id: int, amount: int, note: str) -> int:
    """Set a balance outright. `/economy set`, and nothing else — this is the
    one path that ignores where the coins came from."""
    amount = max(0, amount)
    await ensure_account(db, guild_id, user_id)
    before = await balance(db, guild_id, user_id)
    await db.execute(
        "UPDATE balances SET balance = ? WHERE guild_id = ? AND user_id = ?", (amount, guild_id, user_id)
    )
    await _ledger(db, guild_id, user_id, amount - before, amount, KIND_ADMIN, note)
    return amount


async def transfer(db, guild_id: int, sender_id: int, recipient_id: int, amount: int, note: str) -> bool:
    """Move cc between two members. Debits first, so a failure loses rather
    than duplicates."""
    if not await spend(db, guild_id, sender_id, amount, KIND_PAY_OUT, note):
        return False
    await earn(db, guild_id, recipient_id, amount, KIND_PAY_IN, note)
    return True


async def history(
    db, guild_id: int, user_id: int, limit: int = 20, *, include_chat: bool = False
) -> list[dict[str, Any]]:
    """Recent ledger entries, newest first.

    Chat income is excluded by default: it is a coin or two at a time and would
    bury the entries someone actually opened the ledger to find.
    """
    clause = "" if include_chat else f" AND kind != '{KIND_CHAT}'"
    rows = await db.fetchall(
        "SELECT delta, balance, kind, note, created_at FROM cc_ledger"
        f" WHERE guild_id = ? AND user_id = ?{clause} ORDER BY id DESC LIMIT ?",
        (guild_id, user_id, limit),
    )
    return [dict(row) for row in rows]


async def richest(db, guild_id: int, limit: int = 100) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT user_id, balance, lifetime_earned FROM balances"
        " WHERE guild_id = ? AND (balance > 0 OR lifetime_earned > 0)"
        " ORDER BY balance DESC, lifetime_earned DESC LIMIT ?",
        (guild_id, limit),
    )
    return [dict(row) for row in rows]


async def rank_of(db, guild_id: int, user_id: int) -> int:
    """1-based place on the rich list."""
    own = await balance(db, guild_id, user_id)
    return int(await db.fetchval(
        "SELECT COUNT(*) + 1 FROM balances WHERE guild_id = ? AND balance > ?", (guild_id, own), default=1
    ))


async def circulating(db, guild_id: int) -> int:
    return int(await db.fetchval(
        "SELECT COALESCE(SUM(balance), 0) FROM balances WHERE guild_id = ?", (guild_id,), default=0
    ))


async def _ledger(
    db, guild_id: int, user_id: int, delta: int, new_balance: int, kind: str, note: str | None
) -> None:
    await db.execute(
        "INSERT INTO cc_ledger (guild_id, user_id, delta, balance, kind, note, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (guild_id, user_id, delta, new_balance, kind, note, time.time()),
    )
