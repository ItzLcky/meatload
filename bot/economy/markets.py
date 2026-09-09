"""Culshi: creating, trading, and settling prediction markets.

The money rules from `bank.py` carry over, plus two of its own:

* **One asyncio lock per market.** Pricing depends on `q_yes`/`q_no`, so a trade
  is unavoidably read-compute-write. The lock makes that sequence atomic within
  the process, and every write additionally carries a `WHERE q_yes = ? AND
  q_no = ?` guard so a stale price fails loudly instead of silently trading at
  the wrong number.
* **Settlement flips the status first.** A market is marked resolved by a
  conditional UPDATE, and only a caller that wins that UPDATE pays anybody. Two
  people hitting resolve at once therefore cannot pay the winners twice.

`pot` is the escrow and the thing to keep an eye on. It starts at the creator's
subsidy and tracks the LMSR cost function exactly, which is what guarantees it
always covers the winning side — see `resolve` for why that holds.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..utils.errors import FriendlyError
from . import bank, lmsr

log = logging.getLogger(__name__)

OPEN = "open"
CLOSED = "closed"
RESOLVED = "resolved"
CANCELLED = "cancelled"

TRADEABLE = (OPEN,)
SETTLEABLE = (OPEN, CLOSED)

MIN_SUBSIDY = 100
MAX_SUBSIDY = 1_000_000
DEFAULT_SUBSIDY = 1000

MIN_DURATION = 300  # five minutes
MAX_DURATION = 90 * 86400
DEFAULT_DURATION = 7 * 86400

MAX_QUESTION = 200

_locks: dict[int, asyncio.Lock] = {}


def _side(side: str) -> str:
    """Normalise a side, refusing anything that isn't yes/no.

    Load-bearing: `sell` builds a column name from this, and both sides of a
    trade branch on it, so "not yes" must never quietly mean "no".
    """
    side = (side or "").strip().lower()
    if side not in (lmsr.YES, lmsr.NO):
        raise FriendlyError("Pick a side: `yes` or `no`.")
    return side


def lock_for(market_id: int) -> asyncio.Lock:
    lock = _locks.get(market_id)
    if lock is None:
        lock = _locks[market_id] = asyncio.Lock()
    return lock


# ── reads ────────────────────────────────────────────────────────────────────


async def get(db, market_id: int, guild_id: int | None = None) -> dict[str, Any] | None:
    row = await db.fetchone("SELECT * FROM culshi_markets WHERE id = ?", (market_id,))
    if row is None:
        return None
    market = dict(row)
    if guild_id is not None and market["guild_id"] != guild_id:
        return None  # markets are per-server; don't leak another server's
    return market


async def listing(db, guild_id: int, status: str | None = OPEN, limit: int = 100) -> list[dict[str, Any]]:
    if status is None:
        rows = await db.fetchall(
            "SELECT * FROM culshi_markets WHERE guild_id = ? ORDER BY id DESC LIMIT ?", (guild_id, limit)
        )
    else:
        rows = await db.fetchall(
            "SELECT * FROM culshi_markets WHERE guild_id = ? AND status = ?"
            " ORDER BY closes_at ASC LIMIT ?",
            (guild_id, status, limit),
        )
    return [dict(row) for row in rows]


async def about(db, guild_id: int, subject_id: int, limit: int = 100) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT * FROM culshi_markets WHERE guild_id = ? AND subject_id = ? ORDER BY id DESC LIMIT ?",
        (guild_id, subject_id, limit),
    )
    return [dict(row) for row in rows]


async def position(db, market_id: int, user_id: int) -> dict[str, int]:
    row = await db.fetchone(
        "SELECT yes, no, spent FROM culshi_positions WHERE market_id = ? AND user_id = ?",
        (market_id, user_id),
    )
    return dict(row) if row is not None else {"yes": 0, "no": 0, "spent": 0}


async def holdings(db, guild_id: int, user_id: int) -> list[dict[str, Any]]:
    """Every open position a member holds, newest market first."""
    rows = await db.fetchall(
        "SELECT m.*, p.yes, p.no, p.spent FROM culshi_positions p"
        " JOIN culshi_markets m ON m.id = p.market_id"
        " WHERE p.user_id = ? AND m.guild_id = ? AND m.status IN (?, ?) AND (p.yes > 0 OR p.no > 0)"
        " ORDER BY m.closes_at ASC",
        (user_id, guild_id, OPEN, CLOSED),
    )
    return [dict(row) for row in rows]


async def traders(db, market_id: int) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT user_id, yes, no, spent FROM culshi_positions WHERE market_id = ?", (market_id,)
    )
    return [dict(row) for row in rows]


async def recent_trades(db, market_id: int, limit: int = 5) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT * FROM culshi_trades WHERE market_id = ? ORDER BY id DESC LIMIT ?", (market_id, limit)
    )
    return [dict(row) for row in rows]


async def open_count(db, guild_id: int) -> int:
    return int(await db.fetchval(
        "SELECT COUNT(*) FROM culshi_markets WHERE guild_id = ? AND status = ?",
        (guild_id, OPEN),
        default=0,
    ))


# ── lifecycle ────────────────────────────────────────────────────────────────


async def create(
    db,
    guild_id: int,
    creator_id: int,
    question: str,
    *,
    subject_id: int | None = None,
    duration: int = DEFAULT_DURATION,
    subsidy: int = DEFAULT_SUBSIDY,
    channel_id: int | None = None,
) -> dict[str, Any]:
    """Open a market, charging the creator the subsidy that funds its maker.

    The subsidy is not a fee — it is the market maker's bankroll, and whatever
    the maker has not lost to traders comes back at settlement. Making it real
    money is what stops a server filling up with abandoned markets.
    """
    question = question.strip()
    if not question:
        raise FriendlyError("A market needs a question.")
    if len(question) > MAX_QUESTION:
        raise FriendlyError(f"Keep the question under {MAX_QUESTION} characters.")

    now = time.time()
    liquidity = lmsr.liquidity_for_subsidy(subsidy)

    if not await bank.spend(db, guild_id, creator_id, subsidy, bank.KIND_SUBSIDY, f"Seeded: {question}"):
        held = await bank.balance(db, guild_id, creator_id)
        raise FriendlyError(
            f"Seeding a market costs {bank.format_cc(subsidy)} and you have "
            f"{bank.format_cc(held)}. Try a smaller subsidy."
        )

    cursor = await db.execute(
        "INSERT INTO culshi_markets"
        " (guild_id, creator_id, subject_id, question, status, liquidity, subsidy, pot,"
        "  channel_id, created_at, closes_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            guild_id, creator_id, subject_id, question, OPEN, liquidity, subsidy, subsidy,
            channel_id, now, now + duration,
        ),
    )
    market = await get(db, cursor.lastrowid)
    assert market is not None
    return market


async def close(db, market_id: int) -> bool:
    """Stop trading. Returns False if it was already closed or settled."""
    async with lock_for(market_id):
        cursor = await db.execute(
            "UPDATE culshi_markets SET status = ?, closes_at = MIN(closes_at, ?)"
            " WHERE id = ? AND status = ?",
            (CLOSED, time.time(), market_id, OPEN),
        )
        return cursor.rowcount == 1


async def due_to_close(db, now: float | None = None) -> list[dict[str, Any]]:
    """Markets whose deadline has passed but which are still marked open."""
    rows = await db.fetchall(
        "SELECT * FROM culshi_markets WHERE status = ? AND closes_at <= ?", (OPEN, now or time.time())
    )
    return [dict(row) for row in rows]


# ── trading ──────────────────────────────────────────────────────────────────


def _tradeable(market: dict[str, Any], now: float) -> None:
    """Raise unless the market will accept an order right now.

    The deadline is checked here as well as by the background closer, because
    the closer runs on a timer and a market must stop trading the instant it
    expires, not up to a tick later.
    """
    if market["status"] == RESOLVED:
        raise FriendlyError(f"Market #{market['id']} has already settled.")
    if market["status"] == CANCELLED:
        raise FriendlyError(f"Market #{market['id']} was cancelled.")
    if market["status"] != OPEN or market["closes_at"] <= now:
        raise FriendlyError(f"Market #{market['id']} is closed — no more trading before it settles.")


async def buy(db, market_id: int, user_id: int, side: str, contracts: int) -> dict[str, Any]:
    """Buy `contracts` of `side` from the market maker."""
    side = _side(side)
    if contracts < 1:
        raise FriendlyError("Buy at least one contract.")
    if contracts > lmsr.MAX_CONTRACTS:
        raise FriendlyError(f"{lmsr.MAX_CONTRACTS:,} contracts is the most in one order.")

    async with lock_for(market_id):
        market = await get(db, market_id)
        if market is None:
            raise FriendlyError(f"There's no market #{market_id}.")
        _tradeable(market, time.time())

        q_yes, q_no, b = market["q_yes"], market["q_no"], market["liquidity"]
        cost = lmsr.buy_cost(b, q_yes, q_no, side, contracts)

        note = f"#{market_id} buy {contracts} {side.upper()}"
        if not await bank.spend(db, market["guild_id"], user_id, cost, bank.KIND_BUY, note):
            held = await bank.balance(db, market["guild_id"], user_id)
            most = lmsr.affordable(b, q_yes, q_no, side, held)
            hint = (
                f" You could afford **{most:,}** at "
                f"{bank.format_cc(lmsr.buy_cost(b, q_yes, q_no, side, most))}."
                if most
                else ""
            )
            raise FriendlyError(
                f"That costs {bank.format_cc(cost)} and you have {bank.format_cc(held)}.{hint}"
            )

        new_yes = q_yes + (contracts if side == lmsr.YES else 0)
        new_no = q_no + (contracts if side == lmsr.NO else 0)
        cursor = await db.execute(
            "UPDATE culshi_markets SET q_yes = ?, q_no = ?, pot = pot + ?, volume = volume + ?"
            " WHERE id = ? AND q_yes = ? AND q_no = ? AND status = ?",
            (new_yes, new_no, cost, cost, market_id, q_yes, q_no, OPEN),
        )
        if cursor.rowcount != 1:
            # Someone traded between the read and the write. Hand the money back
            # rather than filling at a price that no longer exists.
            await bank.earn(db, market["guild_id"], user_id, cost, bank.KIND_REFUND, f"{note} (not filled)")
            raise FriendlyError("The market moved while that was going through. Try again.")

        await db.execute(
            "INSERT INTO culshi_positions (market_id, user_id, yes, no, spent) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(market_id, user_id) DO UPDATE SET yes = yes + ?, no = no + ?, spent = spent + ?",
            (
                market_id, user_id,
                contracts if side == lmsr.YES else 0, contracts if side == lmsr.NO else 0, cost,
                contracts if side == lmsr.YES else 0, contracts if side == lmsr.NO else 0, cost,
            ),
        )
        return await _record(db, market, user_id, side, "buy", contracts, cost, new_yes, new_no)


async def sell(db, market_id: int, user_id: int, side: str, contracts: int) -> dict[str, Any]:
    """Sell contracts back to the maker at the current price."""
    side = _side(side)
    column = "yes" if side == lmsr.YES else "no"  # only ever one of these two
    if contracts < 1:
        raise FriendlyError("Sell at least one contract.")

    async with lock_for(market_id):
        market = await get(db, market_id)
        if market is None:
            raise FriendlyError(f"There's no market #{market_id}.")
        _tradeable(market, time.time())

        held = await position(db, market_id, user_id)
        owned = held[side]
        if owned < contracts:
            raise FriendlyError(
                f"You hold {owned:,} {side.upper()} contract(s) in #{market_id}, not {contracts:,}."
            )

        q_yes, q_no, b = market["q_yes"], market["q_no"], market["liquidity"]
        proceeds = lmsr.sell_proceeds(b, q_yes, q_no, side, contracts)

        # Shares go first: if anything fails after this the seller is out the
        # contracts, which is recoverable, rather than holding both the
        # contracts and the cash.
        cursor = await db.execute(
            f"UPDATE culshi_positions SET {column} = {column} - ?, spent = spent - ?"
            f" WHERE market_id = ? AND user_id = ? AND {column} >= ?",
            (contracts, proceeds, market_id, user_id, contracts),
        )
        if cursor.rowcount != 1:
            raise FriendlyError("Those contracts are no longer yours to sell.")

        new_yes = q_yes - (contracts if side == lmsr.YES else 0)
        new_no = q_no - (contracts if side == lmsr.NO else 0)
        cursor = await db.execute(
            "UPDATE culshi_markets SET q_yes = ?, q_no = ?, pot = pot - ?, volume = volume + ?"
            " WHERE id = ? AND q_yes = ? AND q_no = ? AND status = ? AND pot >= ?",
            (new_yes, new_no, proceeds, proceeds, market_id, q_yes, q_no, OPEN, proceeds),
        )
        if cursor.rowcount != 1:
            # Undo the position change; the sale simply did not happen.
            await db.execute(
                f"UPDATE culshi_positions SET {column} = {column} + ?, spent = spent + ?"
                f" WHERE market_id = ? AND user_id = ?",
                (contracts, proceeds, market_id, user_id),
            )
            raise FriendlyError("The market moved while that was going through. Try again.")

        await bank.earn(
            db, market["guild_id"], user_id, proceeds, bank.KIND_SELL,
            f"#{market_id} sell {contracts} {side.upper()}",
        )
        return await _record(db, market, user_id, side, "sell", contracts, proceeds, new_yes, new_no)


async def _record(
    db, market: dict[str, Any], user_id: int, side: str, action: str,
    contracts: int, cc: int, new_yes: int, new_no: int,
) -> dict[str, Any]:
    """Write the tape entry and describe the fill back to the caller."""
    b = market["liquidity"]
    old_price = lmsr.price(b, market["q_yes"], market["q_no"], lmsr.YES)
    new_price = lmsr.price(b, new_yes, new_no, lmsr.YES)
    await db.execute(
        "INSERT INTO culshi_trades (market_id, user_id, side, action, contracts, cc, price, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (market["id"], user_id, side, action, contracts, cc, new_price, time.time()),
    )
    return {
        "market_id": market["id"],
        "side": side,
        "action": action,
        "contracts": contracts,
        "cc": cc,
        "average": round(cc / contracts),
        "old_price": old_price,
        "new_price": new_price,
        "q_yes": new_yes,
        "q_no": new_no,
    }


# ── settlement ───────────────────────────────────────────────────────────────


async def resolve(db, market_id: int, outcome: str, resolver_id: int) -> dict[str, Any]:
    """Settle a market and pay everybody out.

    Winning contracts pay `lmsr.PAYOUT` cc each, and the pot always covers them:
    the pot equals the LMSR cost function C(q_yes, q_no) exactly — the subsidy
    is C(0, 0) by construction, and every trade moves the pot by the change in C
    — and C(q) is never less than `max(q_yes, q_no) · PAYOUT`. Rounding only
    helps: buys round up into the pot, sells round down out of it.

    Whatever is left goes back to the creator. That residue is the market
    maker's profit or loss, and it is bounded below by zero and above by the
    subsidy, which is exactly what subsidising an LMSR is supposed to cost.

    Payouts are individual credits rather than one atomic write, so a crash
    partway through leaves some winners unpaid on a market marked settled.
    That is the deliberate direction to fail in — the alternative ordering
    risks paying somebody twice — and the ledger records exactly who was
    reached, so the rest can be topped up with `/economy give`.
    """
    if outcome not in (lmsr.YES, lmsr.NO, CANCELLED):
        raise FriendlyError("Resolve to `yes`, `no`, or `cancel`.")

    async with lock_for(market_id):
        market = await get(db, market_id)
        if market is None:
            raise FriendlyError(f"There's no market #{market_id}.")
        if market["status"] == RESOLVED:
            raise FriendlyError(f"Market #{market_id} already settled as **{market['outcome'].upper()}**.")
        if market["status"] == CANCELLED:
            raise FriendlyError(f"Market #{market_id} was already cancelled.")

        status = CANCELLED if outcome == CANCELLED else RESOLVED
        now = time.time()
        # Winning this UPDATE is what grants the right to pay out. A second
        # caller finds the status changed and stops here.
        cursor = await db.execute(
            "UPDATE culshi_markets SET status = ?, outcome = ?, resolved_at = ?, resolver_id = ?"
            " WHERE id = ? AND status IN (?, ?)",
            (status, outcome, now, resolver_id, market_id, OPEN, CLOSED),
        )
        if cursor.rowcount != 1:
            raise FriendlyError(f"Market #{market_id} was settled by someone else a moment ago.")

        positions = await traders(db, market_id)
        pot = market["pot"]
        guild_id = market["guild_id"]

        if outcome == CANCELLED:
            payouts = _cancel_refunds(positions, pot)
            kind, label = bank.KIND_REFUND, f"#{market_id} cancelled — refund"
        else:
            payouts = {
                row["user_id"]: lmsr.payout_for(outcome, row["yes"], row["no"])
                for row in positions
                if lmsr.payout_for(outcome, row["yes"], row["no"]) > 0
            }
            kind, label = bank.KIND_SETTLE, f"#{market_id} settled {outcome.upper()}"

        paid = 0
        for user_id, amount in sorted(payouts.items()):
            if amount <= 0:
                continue
            await bank.earn(db, guild_id, user_id, amount, kind, label)
            paid += amount

        residue = pot - paid
        if residue < 0:
            # Cannot happen given the invariant above; if it ever does, the pot
            # is wrong and that is worth knowing about rather than papering over.
            log.error("Market %s paid %s out of a %s pot", market_id, paid, pot)
            residue = 0
        if residue > 0:
            await bank.earn(
                db, guild_id, market["creator_id"], residue, bank.KIND_SETTLE,
                f"#{market_id} market-maker return",
            )

        await db.execute("UPDATE culshi_markets SET pot = 0 WHERE id = ?", (market_id,))
        # Drop the lock: nothing can trade a settled market, so a caller that
        # arrives later and makes a fresh one still gets refused on status.
        _locks.pop(market_id, None)

        return {
            "market": {**market, "status": status, "outcome": outcome},
            "payouts": payouts,
            "paid": paid,
            "residue": residue,
            "traders": len(positions),
        }


def _cancel_refunds(positions: list[dict[str, Any]], pot: int) -> dict[int, int]:
    """Refund each trader's cost basis, scaled down if the pot cannot cover it.

    Someone who already sold at a profit has a negative basis and gets nothing
    back — they took their money out at the market price and keep it. The
    scaling is defensive: cancelling after heavy two-way trading is the one case
    where cost bases can add up to more than the pot still holds.
    """
    owed = {row["user_id"]: row["spent"] for row in positions if row["spent"] > 0}
    total = sum(owed.values())
    if total <= pot or total == 0:
        return owed
    return {user_id: int(amount * pot // total) for user_id, amount in owed.items()}
