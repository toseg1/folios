from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import yfinance as yf

from folios import seed

# Kept in sync by hand with seed.py's private _INSTRUMENT_COLUMNS /
# _ALIAS_COLUMNS — same pattern as valuations.py's own
# VALUATION_CSV_COLUMNS, each file owns the shape of the config row it
# writes rather than reaching into another module's internals.
INSTRUMENT_CSV_COLUMNS = (
    "instrument_id", "isin", "yf_symbol", "name", "asset_class", "instrument_type",
    "currency", "region", "sector", "issuer", "domicile_country", "protection_type",
    "is_pea_eligible", "price_source", "is_active",
)
ALIAS_CSV_COLUMNS = ("alias", "source", "instrument_id")

REQUIRED_NEW_INSTRUMENT_FIELDS = ("Name", "Yahoo ticker", "Asset class", "Currency")


class YFinanceTickerValidator:
    """The empty-result-is-a-hard-error probe from build-plan step 16: a
    silently price-less instrument looks fine and quietly understates
    net worth, so acceptance is gated on 5 days of real history."""

    def has_history(self, ticker: str) -> bool:
        try:
            history = yf.Ticker(ticker).history(period="5d")
        except Exception:
            return False
        return not history.empty


@dataclass
class NewInstrumentResult:
    instrument_id: str | None = None
    alias: str | None = None
    error: str | None = None


@dataclass
class FixTickerResult:
    instrument_id: str | None = None
    error: str | None = None


def _slugify_ticker(ticker: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ticker.strip().upper()).strip("-")
    return slug or "INSTRUMENT"


def _unique_instrument_id(conn: psycopg.Connection, base: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT instrument_id FROM core.instruments WHERE instrument_id LIKE %s",
            (f"{base}%",),
        )
        existing = {row[0] for row in cur.fetchall()}
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def _append_row(path: Path, columns: tuple[str, ...], row: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def create_from_form_fields(
    conn: psycopg.Connection,
    fields: dict[str, str],
    validator: Any = None,
    config_dir: Path | None = None,
) -> NewInstrumentResult:
    """Build-plan step 16, the phone-submission path: validate the
    ticker, write instruments.csv + aliases.csv (config is the seed —
    an instrument existing only in the database would vanish on
    `folios rebuild`), then reconcile immediately via seed.seed() so
    it's usable without waiting for the next `folios init`."""
    missing = [label for label in REQUIRED_NEW_INSTRUMENT_FIELDS if not fields.get(label)]
    if missing:
        return NewInstrumentResult(error=f"missing required field(s): {', '.join(missing)}")

    ticker = fields["Yahoo ticker"].strip()
    validator = validator or YFinanceTickerValidator()
    if not validator.has_history(ticker):
        return NewInstrumentResult(
            error=f"ticker {ticker!r} has no price history on Yahoo Finance"
        )

    config_dir = config_dir if config_dir is not None else seed.CONFIG_DIR
    instrument_id = _unique_instrument_id(conn, _slugify_ticker(ticker))
    alias = instrument_id

    instrument_row = {
        "instrument_id": instrument_id,
        "isin": fields.get("ISIN") or "",
        "yf_symbol": ticker,
        "name": fields["Name"].strip(),
        "asset_class": fields["Asset class"].strip(),
        "instrument_type": "",
        "currency": fields["Currency"].strip(),
        "region": fields.get("Region") or "",
        "sector": fields.get("Sector") or "",
        "issuer": "",
        "domicile_country": "",
        "protection_type": "",
        "is_pea_eligible": "",
        "price_source": "yfinance",
        "is_active": "true",
    }
    alias_row = {"alias": alias, "source": "manual", "instrument_id": instrument_id}

    _append_row(config_dir / "instruments.csv", INSTRUMENT_CSV_COLUMNS, instrument_row)
    _append_row(config_dir / "aliases.csv", ALIAS_CSV_COLUMNS, alias_row)

    try:
        seed.seed(conn, config_dir)
    except seed.SeedValidationError as exc:
        return NewInstrumentResult(error="; ".join(exc.errors))

    return NewInstrumentResult(instrument_id=instrument_id, alias=alias)


def fix_ticker(
    conn: psycopg.Connection,
    instrument_id: str,
    ticker: str,
    validator: Any = None,
    config_dir: Path | None = None,
) -> FixTickerResult:
    """`folios fix-ticker` — supplies a yf_symbol for an instrument
    `folios status` flagged as missing one, or switches a manually-priced
    instrument over to yfinance now that a real ticker exists."""
    validator = validator or YFinanceTickerValidator()
    if not validator.has_history(ticker):
        return FixTickerResult(error=f"ticker {ticker!r} has no price history on Yahoo Finance")

    config_dir = config_dir if config_dir is not None else seed.CONFIG_DIR
    path = config_dir / "instruments.csv"
    rows = seed.load_instruments(path)

    found = False
    for row in rows:
        if row.get("instrument_id") == instrument_id:
            row["yf_symbol"] = ticker
            row["price_source"] = "yfinance"
            found = True
            break

    if not found:
        return FixTickerResult(error=f"{instrument_id} is not in {path}")

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=INSTRUMENT_CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col) or "" for col in INSTRUMENT_CSV_COLUMNS})

    try:
        seed.seed(conn, config_dir)
    except seed.SeedValidationError as exc:
        return FixTickerResult(error="; ".join(exc.errors))

    return FixTickerResult(instrument_id=instrument_id)
