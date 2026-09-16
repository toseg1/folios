from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd
import psycopg
import tenacity
import yfinance as yf

from folios import validate
from folios.models import QUANTITY_SIGN


class YFinancePriceProvider:
    """auto_adjust=False deliberately — storing both close_price and
    adj_close, not just one adjusted series (build-plan step 8)."""

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(3),
        wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def _download(self, symbols: list[str], since: date) -> pd.DataFrame:
        return yf.download(
            symbols,
            start=since.isoformat(),
            auto_adjust=False,
            progress=False,
            group_by="ticker",
            threads=True,
        )

    def fetch_prices(
        self, tickers: dict[str, str], since: date
    ) -> dict[str, dict[date, tuple[Decimal, Decimal | None]]]:
        if not tickers or since > date.today():
            return {}

        # One batched call for every ticker, per build-plan step 8 — Yahoo
        # rate-limits per request, so looping per instrument is out. The
        # tradeoff: an instrument whose own gap is small still gets
        # re-fetched back to the earliest gap among ALL instruments; the
        # upsert makes that harmless, just some wasted bandwidth.
        symbols = sorted(set(tickers.values()))
        raw = self._download(symbols, since)
        if raw.empty:
            return {}

        available = set(raw.columns.get_level_values(0))
        result: dict[str, dict[date, tuple[Decimal, Decimal | None]]] = {}
        for instrument_id, symbol in tickers.items():
            if symbol not in available:
                continue
            per_date: dict[date, tuple[Decimal, Decimal | None]] = {}
            for ts, row in raw[symbol].iterrows():
                close = row.get("Close")
                if pd.isna(close):
                    continue
                adj_close = row.get("Adj Close")
                per_date[ts.date()] = (
                    Decimal(str(close)),
                    None if pd.isna(adj_close) else Decimal(str(adj_close)),
                )
            if per_date:
                result[instrument_id] = per_date
        return result


def instruments_ever_held(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Every instrument that has ever affected a position — not just
    currently held ones (build-plan step 8: "gap detection over
    instruments ever held")."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT i.instrument_id, i.yf_symbol, i.price_source
            FROM core.instruments i
            JOIN core.transactions t ON t.instrument_id = i.instrument_id
            WHERE t.txn_type = ANY(%s)
            """,
            (list(QUANTITY_SIGN),),
        )
        return [
            {"instrument_id": r[0], "yf_symbol": r[1], "price_source": r[2]}
            for r in cur.fetchall()
        ]


def _last_price_date(conn: psycopg.Connection, instrument_id: str) -> date | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(price_date) FROM core.prices WHERE instrument_id = %s",
            (instrument_id,),
        )
        (last,) = cur.fetchone()
    return last


def _first_trade_date(conn: psycopg.Connection, instrument_id: str) -> date | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT min(trade_date) FROM core.transactions WHERE instrument_id = %s",
            (instrument_id,),
        )
        (first,) = cur.fetchone()
    return first


def determine_tickers_to_fetch(
    conn: psycopg.Connection,
) -> tuple[dict[str, str], date | None, list[str]]:
    """Returns (instrument_id -> yf_symbol, the earliest gap-start across
    all of them, warnings). price_source='manual' instruments are
    skipped silently — never reported as missing, per build-plan step 8."""
    tickers: dict[str, str] = {}
    warnings: list[str] = []
    earliest: date | None = None

    for row in instruments_ever_held(conn):
        if row["price_source"] != "yfinance":
            continue
        if not row["yf_symbol"]:
            warnings.append(
                f"{row['instrument_id']}: price_source=yfinance but no "
                f"yf_symbol is set"
            )
            continue

        tickers[row["instrument_id"]] = row["yf_symbol"]
        last = _last_price_date(conn, row["instrument_id"])
        start = (
            last + timedelta(days=1)
            if last is not None
            else _first_trade_date(conn, row["instrument_id"])
        )
        if start is not None and (earliest is None or start < earliest):
            earliest = start

    return tickers, earliest, warnings


_STORE_SQL = """
    INSERT INTO core.prices
        (instrument_id, price_date, close_price, adj_close, currency, source)
    VALUES
        (%(instrument_id)s, %(price_date)s, %(close_price)s, %(adj_close)s,
         %(currency)s, 'yfinance')
    ON CONFLICT (instrument_id, price_date) DO UPDATE SET
        close_price = EXCLUDED.close_price,
        adj_close = EXCLUDED.adj_close
"""


def store_prices(
    conn: psycopg.Connection,
    prices_by_instrument: dict[str, dict[date, tuple[Decimal, Decimal | None]]],
) -> int:
    currencies = validate.instrument_currencies(conn)
    rows = [
        {
            "instrument_id": instrument_id,
            "price_date": price_date,
            "close_price": close,
            "adj_close": adj_close,
            "currency": currencies.get(instrument_id),
        }
        for instrument_id, by_date in prices_by_instrument.items()
        for price_date, (close, adj_close) in by_date.items()
    ]
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(_STORE_SQL, row)
    conn.commit()
    return len(rows)


def refresh_prices(
    conn: psycopg.Connection, provider: Any = None
) -> tuple[int, list[str]]:
    provider = provider or YFinancePriceProvider()
    tickers, earliest, warnings = determine_tickers_to_fetch(conn)
    if not tickers or earliest is None:
        return 0, warnings
    prices = provider.fetch_prices(tickers, earliest)
    stored = store_prices(conn, prices)
    return stored, warnings
