from __future__ import annotations

from datetime import date
from decimal import ROUND_DOWN, Decimal
from typing import Any

import psycopg


class NoAllocationError(Exception):
    """No config/account_allocations.csv snapshot covers this account on
    or before the requested date."""


class MissingPriceError(Exception):
    """One of the account's target instruments has no core.prices row on
    or before the requested date, so its quantity can't be computed."""


def current_allocation(
    conn: psycopg.Connection, account_id: str, as_of: date
) -> list[tuple[str, Decimal]]:
    """(instrument_id, weight) pairs from the latest as_of_date <= as_of
    snapshot for this account — same "latest on or before" lookup as
    fx.resolve_rate_with_date, just against core.account_target_allocations
    instead of core.fx_rates."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT instrument_id, weight
            FROM core.account_target_allocations
            WHERE account_id = %s AND as_of_date = (
                SELECT MAX(as_of_date)
                FROM core.account_target_allocations
                WHERE account_id = %s AND as_of_date <= %s
            )
            ORDER BY instrument_id
            """,
            (account_id, account_id, as_of),
        )
        rows = cur.fetchall()
    if not rows:
        raise NoAllocationError(
            f"no target allocation covers {account_id} on or before "
            f"{as_of.isoformat()} — add a block to "
            f"config/account_allocations.csv, or give this row a symbol"
        )
    return [(instrument_id, weight) for instrument_id, weight in rows]


def resolve_price_with_date(
    conn: psycopg.Connection, instrument_id: str, as_of: date
) -> tuple[Decimal, date] | None:
    """Latest core.prices row on or before as_of, or None if there isn't
    one yet — an expected, gracefully-handled case here (unlike FX, where
    a missing rate is always an error)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT close_price, price_date FROM core.prices
            WHERE instrument_id = %s AND price_date <= %s
            ORDER BY price_date DESC
            LIMIT 1
            """,
            (instrument_id, as_of),
        )
        row = cur.fetchone()
    return (row[0], row[1]) if row else None


def split_amount(
    total: Decimal, weights: list[tuple[str, Decimal]]
) -> list[tuple[str, Decimal]]:
    """Largest-remainder allocation in whole cents: the parts always sum
    exactly to `total`, regardless of how each weight's share rounds.
    Deterministic tie-break on instrument_id, so a rebuild reproduces the
    same split every time."""
    total_cents = int((total * 100).to_integral_value(rounding=ROUND_DOWN))

    shares: list[list[Any]] = []
    allocated = 0
    for instrument_id, weight in weights:
        exact_cents = total * weight * 100
        floor_cents = int(exact_cents.to_integral_value(rounding=ROUND_DOWN))
        shares.append([instrument_id, floor_cents, exact_cents - floor_cents])
        allocated += floor_cents

    remainder = total_cents - allocated
    if remainder >= 0:
        order = sorted(shares, key=lambda s: (-s[2], s[0]))
        for i in range(remainder):
            order[i][1] += 1
    else:
        order = sorted(shares, key=lambda s: (s[2], s[0]))
        for i in range(-remainder):
            order[i][1] -= 1

    return [
        (instrument_id, Decimal(cents) / 100) for instrument_id, cents, _ in shares
    ]


def expand_contribution(
    conn: psycopg.Connection, account_id: str, as_of: date, total: Decimal
) -> list[dict[str, Any]]:
    """A general contribution's raw `gross` -> one row per target
    instrument, with its own euro amount, resolved price, and derived
    quantity. Raises if the account has no allocation, or if any target
    instrument has no price yet — callers (validate.py, loader.py) should
    check this ahead of time so both agree before anything is inserted."""
    weights = current_allocation(conn, account_id, as_of)
    rows = []
    for instrument_id, amount in split_amount(total, weights):
        resolved = resolve_price_with_date(conn, instrument_id, as_of)
        if resolved is None:
            raise MissingPriceError(
                f"no price for {instrument_id} on or before {as_of.isoformat()} "
                f"— run `folios value`/`folios prices` first"
            )
        price, _price_date = resolved
        quantity = (amount / price).quantize(Decimal("0.000001"))
        rows.append(
            {
                "instrument_id": instrument_id,
                "quantity": quantity,
                "price": price,
                "gross_amount": amount,
                "net_amount": -amount,
            }
        )
    return rows
