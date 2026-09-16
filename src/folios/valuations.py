from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg

from folios import validate

REPO_ROOT = validate.REPO_ROOT
DEFAULT_VALUATIONS_PATH = REPO_ROOT / "data" / "manual" / "valuations.csv"

VALUATION_CSV_COLUMNS = ("date", "symbol", "price", "currency")


class ValuationError(Exception):
    """folios value refuses to write anything if any row fails validation
    — same all-or-nothing rule as the transaction loader."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass
class ValuationResult:
    rows_read: int = 0
    rows_stored: int = 0


def read_rows(path: Path) -> list[tuple[int, dict[str, Any]]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        # The header is line 1, so the first data row is line 2.
        return [(i + 2, row) for i, row in enumerate(reader)]


def _instrument_price_sources(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT instrument_id, price_source FROM core.instruments")
        return dict(cur.fetchall())


_STORE_SQL = """
    INSERT INTO core.prices
        (instrument_id, price_date, close_price, adj_close, currency, source)
    VALUES
        (%(instrument_id)s, %(price_date)s, %(close_price)s, NULL,
         %(currency)s, 'manual')
    ON CONFLICT (instrument_id, price_date) DO UPDATE SET
        close_price = EXCLUDED.close_price,
        currency = EXCLUDED.currency
"""


def load_valuations(conn: psycopg.Connection, path: Path) -> ValuationResult:
    """Every rule from build-plan step 8b: symbol must resolve, the
    instrument must have price_source='manual', date must not be in the
    future. No new table, no new transaction type — a manual price is
    exactly what it is, stored in core.prices with source='manual'."""
    aliases = validate.alias_to_instrument(conn)
    price_sources = _instrument_price_sources(conn)
    location = validate.relative_path(path)

    rows = read_rows(path)
    errors: list[str] = []
    to_store: list[dict[str, Any]] = []

    for line, raw in rows:
        where = f"{location}:{line}"

        date_str = (raw.get("date") or "").strip()
        try:
            valuation_date = date.fromisoformat(date_str)
        except ValueError:
            errors.append(f"{where}: date={date_str!r} does not parse as YYYY-MM-DD")
            continue
        if valuation_date > date.today():
            errors.append(f"{where}: date={valuation_date.isoformat()} is in the future")
            continue

        symbol = (raw.get("symbol") or "").strip()
        instrument_id = aliases.get(symbol)
        if instrument_id is None:
            errors.append(
                f"{where}: symbol={symbol!r} does not resolve via instrument_aliases"
            )
            continue

        source = price_sources.get(instrument_id)
        if source != "manual":
            errors.append(
                f"{where}: {symbol} (instrument_id={instrument_id}) has "
                f"price_source={source!r}, not 'manual'"
            )
            continue

        price_str = (raw.get("price") or "").strip()
        try:
            price = Decimal(price_str)
        except InvalidOperation:
            errors.append(f"{where}: price={price_str!r} is not a valid number")
            continue
        if price < 0:
            errors.append(f"{where}: price={price} is negative")
            continue

        currency = (raw.get("currency") or "").strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            errors.append(f"{where}: currency={currency!r} is not a 3-letter ISO code")
            continue

        to_store.append(
            {
                "instrument_id": instrument_id,
                "price_date": valuation_date,
                "close_price": price,
                "currency": currency,
            }
        )

    if errors:
        raise ValuationError(errors)

    with conn.cursor() as cur:
        for row in to_store:
            cur.execute(_STORE_SQL, row)
    conn.commit()

    return ValuationResult(rows_read=len(rows), rows_stored=len(to_store))


def manual_priced_instruments(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Every price_source='manual' instrument, with its most recent
    manual valuation date (NULL if it has never had one)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.instrument_id, i.name, max(p.price_date) AS last_valued
            FROM core.instruments i
            LEFT JOIN core.prices p
                ON p.instrument_id = i.instrument_id AND p.source = 'manual'
            WHERE i.price_source = 'manual'
            GROUP BY i.instrument_id, i.name
            """
        )
        return [
            {"instrument_id": r[0], "name": r[1], "last_valued": r[2]}
            for r in cur.fetchall()
        ]
