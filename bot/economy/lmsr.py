"""Pricing for Culshi markets: Hanson's logarithmic market scoring rule.

A market is one yes/no question. Trading it means buying **contracts**, and a
contract pays `PAYOUT` cc if its side turns out to be right and nothing if it
doesn't — so a contract's price is the market's probability, quoted in cc.
That is the Kalshi shape, and it is why prices here always sit between 1 and 99.

There is no order book. An LMSR market maker quotes both sides continuously
from a single cost function::

    C(q_yes, q_no) = b · ln(e^(q_yes/b) + e^(q_no/b))

and a trade costs whatever it moves C by. That buys three properties worth
having in a server of a dozen people:

* Someone who wants to trade at 3am never has to find a counterparty.
* The price moves with the buying, so it reads as a live probability.
* The maker's worst case is bounded by `b · ln 2 · PAYOUT`, and nothing else.

That bound is the whole funding model: the creator posts exactly that much cc
as a subsidy, `b` is derived from it, and the market can never pay out more
than the subsidy plus what traders put in. A market cannot print cc.

Every function here is pure and works in whole cc at the boundary: costs round
up, proceeds round down. The house keeps the fraction of a coin, which is what
keeps the escrow solvent no matter how the rounding falls.
"""

from __future__ import annotations

import math

PAYOUT = 100  # cc a winning contract settles at
LN2 = math.log(2)

# Prices are clamped off the rails so a lopsided market still quotes something
# tradeable, and so "sold out" never shows as a free contract.
MIN_PRICE = 1
MAX_PRICE = PAYOUT - MIN_PRICE

# Beyond this the exponentials stop being interesting and start being a way to
# overflow a float. No real market needs a six-figure position on one side.
MAX_CONTRACTS = 100_000

YES = "yes"
NO = "no"


def liquidity_for_subsidy(subsidy_cc: int) -> float:
    """The `b` a given subsidy buys.

    Inverts the LMSR loss bound, so the subsidy is spent exactly: a market
    funded with 500 cc can lose at most 500 cc to its traders.
    """
    return subsidy_cc / (PAYOUT * LN2)


def subsidy_for_liquidity(b: float) -> float:
    """The maker's worst case at liquidity `b`, in cc. Inverse of the above."""
    return b * PAYOUT * LN2


def cost(b: float, q_yes: float, q_no: float) -> float:
    """The cost function C, in cc.

    Written as `hi + b·ln(1 + e^(-|Δ|/b))` rather than the textbook form: the
    exponent is never positive, so a market that has run far to one side prices
    the same as one that hasn't instead of overflowing.
    """
    hi, lo = (q_yes, q_no) if q_yes >= q_no else (q_no, q_yes)
    return PAYOUT * (hi + b * math.log1p(math.exp((lo - hi) / b)))


def price(b: float, q_yes: float, q_no: float, side: str = YES) -> int:
    """What one more contract of `side` costs right now, in whole cc.

    This is the marginal price — the number shown as the market's odds. An
    actual trade of any size costs more than `n` times this, because buying
    moves the price; use `buy_cost` for what someone will really pay.
    """
    other = q_no if side == YES else q_yes
    mine = q_yes if side == YES else q_no
    z = (other - mine) / b
    if z > 60:  # e^60 already dwarfs any price; past it the answer is "1 cc"
        return MIN_PRICE
    if z < -60:
        return MAX_PRICE
    raw = PAYOUT / (1 + math.exp(z))
    return min(MAX_PRICE, max(MIN_PRICE, round(raw)))


def probability(b: float, q_yes: float, q_no: float) -> float:
    """The market's implied chance of YES, as a fraction. For progress bars."""
    return price(b, q_yes, q_no, YES) / PAYOUT


def _delta(b: float, q_yes: int, q_no: int, side: str, contracts: int) -> float:
    """How much C moves when `contracts` of `side` are added (may be negative)."""
    if side == YES:
        after = cost(b, q_yes + contracts, q_no)
    else:
        after = cost(b, q_yes, q_no + contracts)
    return after - cost(b, q_yes, q_no)


def buy_cost(b: float, q_yes: int, q_no: int, side: str, contracts: int) -> int:
    """What buying `contracts` costs, in whole cc, rounded up.

    Always at least 1 cc: a contract that could win 100 is never free, however
    hopeless the side looks.
    """
    if contracts <= 0:
        return 0
    return max(1, math.ceil(_delta(b, q_yes, q_no, side, contracts)))


def sell_proceeds(b: float, q_yes: int, q_no: int, side: str, contracts: int) -> int:
    """What selling `contracts` back to the maker pays, in whole cc, rounded down.

    Selling is buying a negative quantity — the same cost function run
    backwards — so a position can always be closed at the current price without
    waiting for the market to resolve.
    """
    if contracts <= 0:
        return 0
    held = q_yes if side == YES else q_no
    contracts = min(contracts, held)
    return max(0, math.floor(-_delta(b, q_yes, q_no, side, -contracts)))


def affordable(b: float, q_yes: int, q_no: int, side: str, budget: int) -> int:
    """The most contracts of `side` that `budget` cc can buy.

    Binary search rather than algebra: inverting the cost function analytically
    is doable but this stays correct if the rounding rules above ever change,
    and it is what the "Max" button and the "you can afford N" hint quote.
    """
    if budget < 1:
        return 0
    low, high = 0, min(MAX_CONTRACTS, budget)  # a contract never costs under 1 cc
    while low < high:
        mid = (low + high + 1) // 2
        if buy_cost(b, q_yes, q_no, side, mid) <= budget:
            low = mid
        else:
            high = mid - 1
    return low


def position_value(b: float, q_yes: int, q_no: int, yes: int, no: int) -> int:
    """Mark a position to market: what it is worth at the current price.

    Marking at the marginal price rather than at liquidation proceeds, which is
    the convention traders expect. Actually selling a large position moves the
    price against you and pays less; `sell_proceeds` is the honest number for
    that, and the sell confirmation quotes it.
    """
    return yes * price(b, q_yes, q_no, YES) + no * price(b, q_yes, q_no, NO)


def payout_for(outcome: str, yes: int, no: int) -> int:
    """What a position collects at settlement, in cc."""
    if outcome == YES:
        return yes * PAYOUT
    if outcome == NO:
        return no * PAYOUT
    return 0
