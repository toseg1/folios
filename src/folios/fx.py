from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import requests

from folios import seed

# api.frankfurter.app permanently redirects to api.frankfurter.dev/v1 as of
# 2026 — using the new host directly avoids an extra round trip per call.
DEFAULT_BASE_URL = os.environ.get(
    "FRANKFURTER_BASE_URL", "https://api.frankfurter.dev/v1"
)


class FxRateNotFoundError(Exception):
    pass


class FrankfurterFxProvider:
    def __init__(
        self, base_url: str = DEFAULT_BASE_URL, session: requests.Session | None = None
    ):
        self.base_url = base_url
        self.session = session or requests.Session()

    def fetch_rates(
        self, since: date, currencies: set[str]
    ) -> dict[date, dict[str, Decimal]]:
        currencies = {c for c in currencies if c and c != "EUR"}
        if not currencies:
            return {}

        response = self.session.get(
            f"{self.base_url}/{since.isoformat()}..",
            params={"from": "EUR", "to": ",".join(sorted(currencies))},
            timeout=15,
        )
        if response.status_code == 404:
            # Nothing published yet for this range (e.g. `since` is later
            # than the most recent TARGET business day) — not an error,
            # just nothing new to fetch.
            return {}
        response.raise_for_status()
        payload = response.json()

        result: dict[date, dict[str, Decimal]] = {}
        for date_str, rates in payload.get("rates", {}).items():
            result[date.fromisoformat(date_str)] = {
                ccy: Decimal(str(rate)) for ccy, rate in rates.items()
            }
        return result


def currencies_from_config(config_dir: Path | None = None) -> set[str]:
    """Every currency implied by config/ — account base currencies plus
    instrument currencies. EUR is included but never fetched or stored
    (a EUR/EUR rate is always exactly 1, per resolve_rate)."""
    config_dir = config_dir if config_dir is not None else seed.CONFIG_DIR
    accounts = seed.load_accounts(config_dir / "accounts.yml")
    instruments = seed.load_instruments(config_dir / "instruments.csv")

    currencies = {row.get("base_currency") for row in accounts}
    currencies |= {row.get("currency") for row in instruments}
    return {c for c in currencies if c}


def determine_fetch_start(conn: psycopg.Connection, since: date) -> date:
    """--since is a floor, not a fixed start: once rates exist, resume
    the day after whatever was last stored, regardless of --since."""
    with conn.cursor() as cur:
        cur.execute("SELECT max(rate_date) FROM core.fx_rates WHERE base_ccy = 'EUR'")
        (last_date,) = cur.fetchone()
    if last_date is None:
        return since
    return max(since, last_date + timedelta(days=1))


_STORE_SQL = """
    INSERT INTO core.fx_rates (rate_date, base_ccy, quote_ccy, rate, source)
    VALUES (%(rate_date)s, 'EUR', %(quote_ccy)s, %(rate)s, 'ecb')
    ON CONFLICT (rate_date, base_ccy, quote_ccy) DO UPDATE SET rate = EXCLUDED.rate
"""


def store_rates(
    conn: psycopg.Connection, rates_by_date: dict[date, dict[str, Decimal]]
) -> int:
    rows: list[dict[str, Any]] = [
        {"rate_date": rate_date, "quote_ccy": ccy, "rate": rate}
        for rate_date, rates in rates_by_date.items()
        for ccy, rate in rates.items()
    ]
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(_STORE_SQL, row)
    conn.commit()
    return len(rows)


def _lookup_eur_rate(conn: psycopg.Connection, rate_date: date, quote_ccy: str) -> Decimal:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT rate FROM core.fx_rates
            WHERE base_ccy = 'EUR' AND quote_ccy = %s AND rate_date <= %s
            ORDER BY rate_date DESC
            LIMIT 1
            """,
            (quote_ccy, rate_date),
        )
        row = cur.fetchone()
    if row is None:
        raise FxRateNotFoundError(
            f"no EUR/{quote_ccy} rate on or before {rate_date} — "
            f"run `folios fx --since <date>` first"
        )
    return row[0]


def resolve_rate(
    conn: psycopg.Connection, rate_date: date, base_ccy: str, quote_ccy: str
) -> Decimal:
    """The single FX resolution helper — every consumer of core.fx_rates
    goes through this, no exceptions. Most recent rate on or before
    rate_date, never interpolated. A currency against itself is always 1,
    resolved without touching the database."""
    if base_ccy == quote_ccy:
        return Decimal("1")
    if base_ccy == "EUR":
        return _lookup_eur_rate(conn, rate_date, quote_ccy)
    if quote_ccy == "EUR":
        return Decimal("1") / _lookup_eur_rate(conn, rate_date, base_ccy)
    # Cross rate: derive through EUR, per §3 — only one direction is ever stored.
    return _lookup_eur_rate(conn, rate_date, quote_ccy) / _lookup_eur_rate(
        conn, rate_date, base_ccy
    )
