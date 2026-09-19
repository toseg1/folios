from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg
from stockdex import Ticker

from folios.models import QUANTITY_SIGN
from folios.seed import CONFIG_DIR

# justETF's sector/country breakdowns are the two complete aggregate
# dimensions (each sums to ~1). "region" has no stockdex accessor at all
# (and is no longer a folios dimension — see migrations/007).
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

    def fetch_basics(self, isin: str) -> dict[str, str]:
        """justETF's "basics" tab — confirmed live against a real ISIN
        (EUNL / IE00B4L5Y983): a one-row DataFrame, columns are the
        human-readable field labels ("Index", "Fund size", "Legal
        structure", ...), values are raw display strings ("EUR 127,272 m",
        "0.20% p.a.", ...). See parse_basics() for what each label means
        and how it's parsed."""
        ticker = Ticker(isin=isin, security_type="etf")
        row = ticker.justetf_basics.iloc[0]
        return {str(k): str(v) for k, v in row.to_dict().items()}


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


# justETF's own "Legal structure" field (e.g. "ETF") is deliberately never
# parsed here — it's the wrapper-type concept folios already calls
# instrument_type, typed manually at creation, not the deeper structural
# question instrument_fund.legal_structure answers (UCITS_FUND vs.
# UNSECURED_NOTE vs. SCPI — what you actually own if the issuer fails).
# Different questions that happen to share a label; conflating them would
# silently overwrite a structurally load-bearing manual field.
_REPLICATION_LABELS = {
    "physical (full replication)": "PHYSICAL_FULL",
    "physical (optimized sampling)": "PHYSICAL_SAMPLED",
    "physical (sampling)": "PHYSICAL_SAMPLED",
    "synthetic": "SYNTHETIC",
}

_DISTRIBUTION_POLICY_LABELS = {
    "accumulating": "ACCUMULATING",
    "distributing": "DISTRIBUTING",
}


def _dash_to_none(raw: str | None) -> str | None:
    if raw is None:
        return None
    stripped = raw.strip()
    return None if stripped in ("", "-") else stripped


def _parse_percent(raw: str | None) -> Decimal | None:
    """"0.20% p.a." / "10.73%" -> decimal fraction (0.0020 / 0.1073)."""
    stripped = _dash_to_none(raw)
    if stripped is None:
        return None
    stripped = stripped.replace("p.a.", "").strip().rstrip("%").strip()
    try:
        return Decimal(stripped) / Decimal("100")
    except InvalidOperation:
        return None


_MONEY_UNIT_MULTIPLIERS = {"k": Decimal("1e3"), "m": Decimal("1e6"), "bn": Decimal("1e9")}


def _parse_money(raw: str | None) -> Decimal | None:
    """"EUR 127,272 m" -> 127272000000. Currency prefix is dropped —
    fund_size is informational (AUM context), nothing computes with it, so
    the unit ambiguity across funds published in different currencies is
    an accepted limitation rather than a new column."""
    stripped = _dash_to_none(raw)
    if stripped is None:
        return None
    match = re.search(r"([\d,]+(?:\.\d+)?)\s*(k|m|bn)?\s*$", stripped, re.IGNORECASE)
    if not match:
        return None
    try:
        amount = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    unit = (match.group(2) or "").lower()
    return amount * _MONEY_UNIT_MULTIPLIERS.get(unit, Decimal("1"))


def _parse_date(raw: str | None) -> date | None:
    """"25 September 2009" -> date(2009, 9, 25)."""
    stripped = _dash_to_none(raw)
    if stripped is None:
        return None
    try:
        return datetime.strptime(stripped, "%d %B %Y").date()
    except ValueError:
        return None


def parse_basics(
    raw: dict[str, str],
    country_mapping: dict[tuple[str, str], str],
    warnings: list[str],
) -> dict[str, Any]:
    """justETF "basics" label -> folios column, per the confirmed live
    shape (see StockdexExposureProvider.fetch_basics). Pure function, no
    network — every field here is independently parseable from `raw`
    alone, so this is fully unit-testable against a frozen fixture."""
    replication_raw = _dash_to_none(raw.get("Replication"))
    replication = (
        _REPLICATION_LABELS.get(replication_raw.lower(), replication_raw)
        if replication_raw
        else None
    )

    distribution_policy_raw = _dash_to_none(raw.get("Distribution policy"))
    distribution_policy = (
        _DISTRIBUTION_POLICY_LABELS.get(
            distribution_policy_raw.lower(), distribution_policy_raw.upper()
        )
        if distribution_policy_raw
        else None
    )

    distribution_frequency = _dash_to_none(raw.get("Distribution frequency"))
    if distribution_frequency is not None:
        distribution_frequency = distribution_frequency.upper()

    fund_currency = _dash_to_none(raw.get("Fund currency"))
    if fund_currency is not None:
        fund_currency = fund_currency.upper()

    domicile_country = None
    domicile_label = _dash_to_none(raw.get("Fund domicile"))
    if domicile_label is not None:
        domicile_country = country_mapping.get(("country", domicile_label))
        if domicile_country is None:
            warnings.append(
                f"country={domicile_label!r} has no config/exposure_mapping.csv "
                f"row — domicile_country left blank"
            )

    return {
        "benchmark_index": _dash_to_none(raw.get("Index")),
        "investment_focus": _dash_to_none(raw.get("Investment focus")),
        "fund_size": _parse_money(raw.get("Fund size")),
        "ongoing_charges": _parse_percent(raw.get("Total expense ratio")),
        "replication_method": replication,
        "investment_approach": _dash_to_none(raw.get("Investment approach")),
        "sustainability": _dash_to_none(raw.get("Sustainability")),
        "fund_currency": fund_currency,
        "currency_risk": _dash_to_none(raw.get("Currency risk")),
        "volatility_1y_eur": _parse_percent(raw.get("Volatility 1 year (in EUR)")),
        "inception_date": _parse_date(raw.get("Inception/ Listing Date")),
        "distribution_policy": distribution_policy,
        "distribution_frequency": distribution_frequency,
        "domicile_country": domicile_country,
        "issuer": _dash_to_none(raw.get("Fund Provider")),
    }


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


_STORE_BASICS_FUND_SQL = """
    UPDATE core.instrument_fund SET
        benchmark_index = COALESCE(%(benchmark_index)s, benchmark_index),
        investment_focus = COALESCE(%(investment_focus)s, investment_focus),
        fund_size = COALESCE(%(fund_size)s, fund_size),
        ongoing_charges = COALESCE(%(ongoing_charges)s, ongoing_charges),
        replication_method = COALESCE(%(replication_method)s, replication_method),
        investment_approach = COALESCE(%(investment_approach)s, investment_approach),
        sustainability = COALESCE(%(sustainability)s, sustainability),
        fund_currency = COALESCE(%(fund_currency)s, fund_currency),
        currency_risk = COALESCE(%(currency_risk)s, currency_risk),
        volatility_1y_eur = COALESCE(%(volatility_1y_eur)s, volatility_1y_eur),
        inception_date = COALESCE(%(inception_date)s, inception_date),
        distribution_policy = COALESCE(%(distribution_policy)s, distribution_policy),
        distribution_frequency = COALESCE(%(distribution_frequency)s, distribution_frequency)
    WHERE instrument_id = %(instrument_id)s
"""

_STORE_BASICS_INSTRUMENT_SQL = """
    UPDATE core.instruments SET
        domicile_country = COALESCE(%(domicile_country)s, domicile_country),
        issuer = COALESCE(%(issuer)s, issuer)
    WHERE instrument_id = %(instrument_id)s
"""


def store_basics(conn: psycopg.Connection, instrument_id: str, parsed: dict[str, Any]) -> None:
    """Writes parse_basics()'s output onto the existing instrument_fund
    row and the instrument's own domicile_country/issuer — COALESCEd
    against the current value so a field the fetch didn't return (or
    couldn't map, e.g. an unmapped domicile) never clobbers a previously
    good value with NULL. Never touches legal_structure, rhp_years, sri,
    custodian, sfdr_article, justetf_id, or the SCPI-only fields — those
    stay manual."""
    params = {"instrument_id": instrument_id, **parsed}
    with conn.cursor() as cur:
        cur.execute(_STORE_BASICS_FUND_SQL, params)
        cur.execute(_STORE_BASICS_INSTRUMENT_SQL, params)
    conn.commit()


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

        # Same independent-failure treatment as top holdings — TER/fund
        # size/volatility etc. drift over time, so this re-fetches on
        # every refresh rather than only ever populating once at creation.
        fetch_basics = getattr(provider, "fetch_basics", None)
        if fetch_basics is not None:
            try:
                basics_raw = fetch_basics(row["isin"])
            except Exception as exc:  # noqa: BLE001 — scraped page, any failure mode
                result.warnings.append(f"{instrument_id}: basics refresh failed: {exc}")
            else:
                parsed = parse_basics(basics_raw, mapping, result.warnings)
                store_basics(conn, instrument_id, parsed)

    return result
