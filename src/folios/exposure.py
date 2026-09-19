from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg
from stockdex import Ticker

from folios.models import QUANTITY_SIGN
from folios.seed import CONFIG_DIR

# justETF's sector/country breakdowns are the two complete aggregate
# dimensions (each sums to ~1). "region" has no stockdex accessor at all
# (and is no longer a folios dimension — see migrations/010).
DIMENSIONS = ("sector", "country")


class StockdexExposureProvider:
    def fetch_exposure(self, isin: str) -> dict[str, list[tuple[str, Decimal]]]:
        ticker = Ticker(isin=isin, security_type="etf")
        return {
            "sector": _parse_weights(ticker.justetf_holdings_sectors),
            "country": _parse_weights(ticker.justetf_holdings_countries),
        }

    def fetch_top_holdings(self, isin: str) -> list[tuple[str, Decimal]]:
        """Top-10 holdings by company — same accessor family, same
        DataFrame/percentage-column shape as the sector/country calls
        above, confirmed by the same stockdex reading that confirmed
        those (data-model-review §3.2). Ranked list, not a full mapped
        taxonomy, so it doesn't go through config/exposure_mapping.csv."""
        ticker = Ticker(isin=isin, security_type="etf")
        return _parse_weights(ticker.justetf_holdings_companies)


def _parse_weights(df: Any) -> list[tuple[str, Decimal]]:
    column = df.columns[0]
    pairs: list[tuple[str, Decimal]] = []
    for label, row in df.iterrows():
        raw = str(row[column]).strip().rstrip("%")
        try:
            weight = Decimal(raw) / Decimal("100")
        except InvalidOperation:
            continue
        pairs.append((str(label), weight))
    return pairs


def load_exposure_mapping(path: Path) -> dict[tuple[str, str], str]:
    """{(dimension, source_label): code}. A missing file means everything
    falls back to UNMAPPED, which is a safe (visible, not silently
    dropped) default — see folios status.

    Originally built for ETP look-through ingestion only; now dual-purpose
    — new_instrument.py also reuses this same file/shape to map a direct
    (non-look-through) EQUITY's yfinance sector/industry/country onto
    config/dimensions.csv codes, via map_or_unmapped() below."""
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {
            (row["dimension"], row["source_label"]): row["code"]
            for row in csv.DictReader(f)
        }


def map_or_unmapped(
    mapping: dict[tuple[str, str], str], dimension: str, label: str | None
) -> str | None:
    """Same UNMAPPED:<label> convention as store_exposure below, reused
    here so a direct sector/industry/country value is never silently
    dropped when config/exposure_mapping.csv hasn't been filled in for it
    yet. Returns None for a blank/missing label (nothing to map)."""
    if not label:
        return None
    return mapping.get((dimension, label)) or f"UNMAPPED:{label}"


def etps_held(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Currently-held ETP instruments (asset_class=ETP), with isin and
    legal_structure — what determines whether look-through applies."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.instrument_id, i.isin, f.legal_structure,
                   COALESCE(SUM(t.quantity), 0) AS position
            FROM core.instruments i
            LEFT JOIN core.instrument_fund f ON f.instrument_id = i.instrument_id
            LEFT JOIN core.transactions t
                ON t.instrument_id = i.instrument_id
               AND t.txn_type = ANY(%s)
            WHERE i.asset_class = 'ETP'
            GROUP BY i.instrument_id, i.isin, f.legal_structure
            HAVING COALESCE(SUM(t.quantity), 0) > 0
            """,
            (list(QUANTITY_SIGN),),
        )
        return [
            {"instrument_id": r[0], "isin": r[1], "legal_structure": r[2]}
            for r in cur.fetchall()
        ]


_STORE_SQL = """
    INSERT INTO core.etp_exposure
        (instrument_id, as_of_date, dimension, code, label, weight, source)
    VALUES
        (%(instrument_id)s, %(as_of_date)s, %(dimension)s, %(code)s,
         %(label)s, %(weight)s, 'justetf')
    ON CONFLICT (instrument_id, as_of_date, dimension, code) DO UPDATE SET
        label = EXCLUDED.label,
        weight = EXCLUDED.weight
"""


def store_exposure(
    conn: psycopg.Connection,
    instrument_id: str,
    as_of_date: date,
    exposure: dict[str, list[tuple[str, Decimal]]],
    mapping: dict[tuple[str, str], str],
) -> int:
    # A label with no mapping row falls back to a code unique to that
    # label (UNMAPPED:<label>), not a bare "UNMAPPED" — two distinct
    # unmapped labels in the same dimension/instrument/date would
    # otherwise collide on the (instrument, date, dimension, code)
    # primary key and silently overwrite each other. This is the common
    # case, not a rare one: it's exactly what happens before
    # config/exposure_mapping.csv has been filled in at all. A mapping
    # file MAY still explicitly map a label to the literal "UNMAPPED"
    # (e.g. justETF's own "Other" bucket) — that stays a deliberate,
    # single-row choice, not this fallback.
    rows = [
        {
            "instrument_id": instrument_id,
            "as_of_date": as_of_date,
            "dimension": dimension,
            "code": map_or_unmapped(mapping, dimension, label),
            "label": label,
            "weight": weight,
        }
        for dimension, pairs in exposure.items()
        for label, weight in pairs
    ]
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(_STORE_SQL, row)
    conn.commit()
    return len(rows)


_STORE_TOP_HOLDINGS_SQL = """
    INSERT INTO core.etp_top_holdings
        (instrument_id, as_of_date, rank, company_name, weight)
    VALUES
        (%(instrument_id)s, %(as_of_date)s, %(rank)s, %(company_name)s, %(weight)s)
    ON CONFLICT (instrument_id, as_of_date, rank) DO UPDATE SET
        company_name = EXCLUDED.company_name,
        weight = EXCLUDED.weight
"""


def store_top_holdings(
    conn: psycopg.Connection,
    instrument_id: str,
    as_of_date: date,
    holdings: list[tuple[str, Decimal]],
) -> int:
    rows = [
        {
            "instrument_id": instrument_id,
            "as_of_date": as_of_date,
            "rank": i + 1,
            "company_name": company_name,
            "weight": weight,
        }
        for i, (company_name, weight) in enumerate(holdings[:10])
    ]
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(_STORE_TOP_HOLDINGS_SQL, row)
    conn.commit()
    return len(rows)


@dataclass
class ExposureResult:
    refreshed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def refresh_exposure(
    conn: psycopg.Connection,
    provider: Any = None,
    config_dir: Path | None = None,
) -> ExposureResult:
    """folios exposure --refresh. Non-fatal per instrument: justETF is a
    scraped page, not an API, so one failed fetch logs a warning and
    leaves that instrument's last snapshot in place rather than
    aborting the whole run."""
    provider = provider or StockdexExposureProvider()
    config_dir = config_dir if config_dir is not None else CONFIG_DIR
    mapping = load_exposure_mapping(config_dir / "exposure_mapping.csv")
    today = date.today()

    result = ExposureResult()
    for row in etps_held(conn):
        instrument_id = row["instrument_id"]

        if row["legal_structure"] != "UCITS_FUND":
            result.skipped.append(
                f"{instrument_id}: legal_structure={row['legal_structure']!r} "
                f"has no holdings to look through"
            )
            continue
        if not row["isin"]:
            result.skipped.append(f"{instrument_id}: no ISIN set")
            continue

        try:
            exposure = provider.fetch_exposure(row["isin"])
        except Exception as exc:  # noqa: BLE001 — scraped page, any failure mode
            result.warnings.append(f"{instrument_id}: exposure refresh failed: {exc}")
            continue

        store_exposure(conn, instrument_id, today, exposure, mapping)
        result.refreshed.append(instrument_id)

        # Top holdings is a separate accessor call — its own failure
        # mode (or absence, if the provider doesn't implement it) must
        # not roll back the sector/country refresh that already
        # succeeded above.
        fetch_top_holdings = getattr(provider, "fetch_top_holdings", None)
        if fetch_top_holdings is not None:
            try:
                holdings = fetch_top_holdings(row["isin"])
            except Exception as exc:  # noqa: BLE001 — scraped page, any failure mode
                result.warnings.append(
                    f"{instrument_id}: top holdings refresh failed: {exc}"
                )
            else:
                store_top_holdings(conn, instrument_id, today, holdings)

    return result
