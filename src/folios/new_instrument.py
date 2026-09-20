from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import yfinance as yf

from folios import exposure, seed

# Kept in sync by hand with seed.py's private _INSTRUMENT_COLUMNS /
# _ALIAS_COLUMNS — same pattern as valuations.py's own
# VALUATION_CSV_COLUMNS, each file owns the shape of the config row it
# writes rather than reaching into another module's internals.
INSTRUMENT_CSV_COLUMNS = (
    "instrument_id", "isin", "yf_symbol", "ticker", "name", "asset_class",
    "instrument_type", "currency", "sector", "industry", "description", "issuer",
    "domicile_country", "protection_type", "is_pea_eligible", "price_source",
    "is_active",
)
ALIAS_CSV_COLUMNS = ("alias", "source", "instrument_id")

# Column order copied verbatim from seed.py's private _INSTRUMENT_FUND_COLUMNS /
# _INSTRUMENT_BOND_COLUMNS / _INSTRUMENT_CRYPTO_COLUMNS — same "each file owns
# its own shape" convention as above.
FUND_CSV_COLUMNS = (
    "instrument_id", "legal_structure", "is_ucits", "rhp_years",
    "distribution_policy", "ongoing_charges", "sri", "replication_method",
    "swap_counterparty", "uses_sec_lending", "custodian", "sfdr_article",
    "benchmark_index", "justetf_id", "subscription_price", "withdrawal_price",
    "management_company", "property_sector", "occupancy_rate", "distribution_rate",
    "investment_focus", "fund_size", "investment_approach", "sustainability",
    "currency_risk", "fund_currency", "volatility_1y_eur", "inception_date",
    "distribution_frequency",
)
BOND_CSV_COLUMNS = (
    "instrument_id", "coupon_rate", "coupon_frequency", "maturity_date",
    "face_value", "issuer_type", "credit_rating", "seniority", "is_callable",
)
CRYPTO_CSV_COLUMNS = (
    "instrument_id", "chain", "contract_address", "token_standard", "consensus",
    "is_stablecoin", "peg_currency",
)

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


class YFinanceInfoProvider:
    """Best-effort auto-population of sector/industry/country/description
    for EQUITY, from yfinance's .info dict — a stable, well-documented
    field set. NOT used for ETP/FUND: the equivalent justETF fields come
    from exposure.StockdexExposureProvider.fetch_basics() instead (its own
    accessor, confirmed live and wired in the ETP branch below)."""

    def fetch_info(self, ticker: str) -> dict[str, Any]:
        try:
            return dict(yf.Ticker(ticker).info or {})
        except Exception:
            return {}


@dataclass
class NewInstrumentResult:
    instrument_id: str | None = None
    alias: str | None = None
    warnings: list[str] = field(default_factory=list)
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


def _map_dimension(
    mapping: dict[tuple[str, str], str],
    dimension: str,
    label: str | None,
    warnings: list[str],
) -> str | None:
    """Maps a fetched raw label onto a config/dimensions.csv code. Unlike
    etp_exposure's UNMAPPED:<label> fallback (exposure.map_or_unmapped),
    an instrument's sector/industry/domicile_country ARE dimension-checked
    at seed time — an unknown code is a hard SeedValidationError, nothing
    written. So an unmapped label must not be written at all here; it's
    surfaced as a warning instead (add a row to exposure_mapping.csv),
    without blocking instrument creation."""
    if not label:
        return None
    code = mapping.get((dimension, label))
    if code is None:
        warnings.append(
            f"{dimension}={label!r} has no config/exposure_mapping.csv row — left blank"
        )
    return code


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
    info_provider: Any = None,
    basics_provider: Any = None,
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
    # A readable "Name - code" is what actually shows up in the Symbol
    # dropdown (via symbol_choices()) — instrument_id is already
    # de-duplicated above, so this combo is automatically unique too.
    alias = f"{fields['Name'].strip()} - {instrument_id}"

    asset_class = fields["Asset class"].strip()

    # Sector/industry/country/description are no longer typed by hand —
    # for EQUITY they're fetched from yfinance's .info and mapped through
    # config/exposure_mapping.csv, same convention as ETP look-through
    # exposure already uses. Any other asset class (BOND/CRYPTO/a
    # non-listed FUND) has no automatable source for these, so they stay
    # unset unless a caller passes them in `fields` directly (e.g. a
    # future non-phone entry path).
    enrichment_warnings: list[str] = []
    sector = fields.get("Sector") or ""
    industry = ""
    description = ""
    domicile_country = fields.get("Domicile country") or ""
    issuer = fields.get("Issuer") or ""
    # Populated for ETP below, then merged into fund_row further down —
    # every instrument_fund column justETF's "basics" tab can supply
    # (investment_focus, fund_size, ongoing_charges, replication_method,
    # investment_approach, sustainability, fund_currency, currency_risk,
    # volatility_1y_eur, inception_date, benchmark_index,
    # distribution_policy, distribution_frequency).
    fund_auto: dict[str, Any] = {}
    if asset_class == "EQUITY":
        info_provider = info_provider or YFinanceInfoProvider()
        mapping = exposure.load_exposure_mapping(config_dir / "exposure_mapping.csv")
        info = info_provider.fetch_info(ticker)
        sector = (
            _map_dimension(mapping, "sector", info.get("sector"), enrichment_warnings)
            or sector
        )
        industry = (
            _map_dimension(mapping, "industry", info.get("industry"), enrichment_warnings) or ""
        )
        domicile_country = (
            _map_dimension(mapping, "country", info.get("country"), enrichment_warnings)
            or domicile_country
        )
        description = info.get("longBusinessSummary") or ""
    elif asset_class == "ETP":
        isin = fields.get("ISIN") or ""
        if not isin:
            enrichment_warnings.append(
                "no ISIN set — justETF fields not fetched, fill in manually or "
                "add an ISIN and run `folios exposure --refresh` later"
            )
        else:
            basics_provider = basics_provider or exposure.StockdexExposureProvider()
            mapping = exposure.load_exposure_mapping(config_dir / "exposure_mapping.csv")
            try:
                basics_raw = basics_provider.fetch_basics(isin)
            except Exception as exc:  # noqa: BLE001 — scraped page, any failure mode
                enrichment_warnings.append(f"justETF basics fetch failed: {exc}")
            else:
                fund_auto = exposure.parse_basics(basics_raw, mapping, enrichment_warnings)
                domicile_country = fund_auto.get("domicile_country") or domicile_country
                issuer = fund_auto.get("issuer") or issuer

    instrument_row = {
        "instrument_id": instrument_id,
        "isin": fields.get("ISIN") or "",
        "yf_symbol": ticker,
        "ticker": fields.get("Ticker") or "",
        "name": fields["Name"].strip(),
        "asset_class": asset_class,
        "instrument_type": fields.get("Instrument type") or "",
        "currency": fields["Currency"].strip(),
        "sector": sector,
        "industry": industry,
        "description": description,
        "issuer": issuer,
        "domicile_country": domicile_country,
        "protection_type": fields.get("Protection type") or "",
        "is_pea_eligible": fields.get("PEA eligible") or "",
        "price_source": "yfinance",
        "is_active": "true",
    }
    alias_row = {"alias": alias, "source": "manual", "instrument_id": instrument_id}

    _append_row(config_dir / "instruments.csv", INSTRUMENT_CSV_COLUMNS, instrument_row)
    _append_row(config_dir / "aliases.csv", ALIAS_CSV_COLUMNS, alias_row)

    subtype_table = seed.SUBTYPE_TABLE_BY_ASSET_CLASS.get(instrument_row["asset_class"])
    if subtype_table == "instrument_fund":

        def _auto(key: str) -> str:
            value = fund_auto.get(key)
            return "" if value is None else str(value)

        fund_row = {
            "instrument_id": instrument_id,
            "legal_structure": fields.get("Legal structure") or "",
            "is_ucits": fields.get("UCITS") or "",
            "rhp_years": fields.get("RHP (years)") or "",
            "distribution_policy": (
                fields.get("Distribution policy") or _auto("distribution_policy")
            ),
            "ongoing_charges": fields.get("Ongoing charges") or _auto("ongoing_charges"),
            "sri": fields.get("SRI") or "",
            "replication_method": fields.get("Replication method") or _auto("replication_method"),
            "swap_counterparty": fields.get("Swap counterparty") or "",
            "uses_sec_lending": fields.get("Uses securities lending") or "",
            "custodian": fields.get("Custodian") or "",
            "sfdr_article": fields.get("SFDR article") or "",
            "benchmark_index": fields.get("Benchmark index") or _auto("benchmark_index"),
            "justetf_id": fields.get("justETF id") or "",
            "subscription_price": fields.get("Subscription price (SCPI)") or "",
            "withdrawal_price": fields.get("Withdrawal price (SCPI)") or "",
            "management_company": fields.get("Management company (SCPI)") or "",
            "property_sector": fields.get("Property sector (SCPI)") or "",
            "occupancy_rate": fields.get("Occupancy rate (SCPI)") or "",
            "distribution_rate": fields.get("Distribution rate (SCPI)") or "",
            # justETF-sourced, ETP only — auto-fetched via
            # exposure.StockdexExposureProvider.fetch_basics() above; manual
            # entry (if ever present in `fields`) always wins.
            "investment_focus": fields.get("Investment focus") or _auto("investment_focus"),
            "fund_size": fields.get("Fund size") or _auto("fund_size"),
            "investment_approach": (
                fields.get("Investment approach") or _auto("investment_approach")
            ),
            "sustainability": fields.get("Sustainability") or _auto("sustainability"),
            "currency_risk": fields.get("Currency risk") or _auto("currency_risk"),
            "fund_currency": fields.get("Fund currency") or _auto("fund_currency"),
            "volatility_1y_eur": fields.get("Volatility 1y (EUR)") or _auto("volatility_1y_eur"),
            "inception_date": fields.get("Inception date") or _auto("inception_date"),
            "distribution_frequency": fields.get("Distribution frequency")
            or _auto("distribution_frequency"),
        }
        _append_row(config_dir / "instruments_fund.csv", FUND_CSV_COLUMNS, fund_row)
    elif subtype_table == "instrument_bond":
        bond_row = {
            "instrument_id": instrument_id,
            "coupon_rate": fields.get("Coupon rate") or "",
            "coupon_frequency": fields.get("Coupon frequency") or "",
            "maturity_date": fields.get("Maturity date") or "",
            "face_value": fields.get("Face value") or "",
            "issuer_type": fields.get("Issuer type") or "",
            "credit_rating": fields.get("Credit rating") or "",
            "seniority": fields.get("Seniority") or "",
            "is_callable": fields.get("Is callable") or "",
        }
        _append_row(config_dir / "instruments_bond.csv", BOND_CSV_COLUMNS, bond_row)
    elif subtype_table == "instrument_crypto":
        crypto_row = {
            "instrument_id": instrument_id,
            "chain": fields.get("Chain") or "",
            "contract_address": fields.get("Contract address") or "",
            "token_standard": fields.get("Token standard") or "",
            "consensus": fields.get("Consensus") or "",
            "is_stablecoin": fields.get("Is stablecoin") or "",
            "peg_currency": fields.get("Peg currency") or "",
        }
        _append_row(config_dir / "instruments_crypto.csv", CRYPTO_CSV_COLUMNS, crypto_row)

    try:
        result = seed.seed(conn, config_dir)
    except seed.SeedValidationError as exc:
        return NewInstrumentResult(error="; ".join(exc.errors))

    return NewInstrumentResult(
        instrument_id=instrument_id,
        alias=alias,
        warnings=[*enrichment_warnings, *result.warnings],
    )


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
