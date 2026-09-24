from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"

# Dimension-valued fields on each record type, per the build plan: the
# dimension name in core.dimensions is always the field name itself.
ACCOUNT_DIMENSION_FIELDS = ("account_type", "fiscal_envelope", "custody_type")
INSTRUMENT_DIMENSION_FIELDS = (
    "asset_class",
    "sector",
    "industry",
    "instrument_type",
    "protection_type",
)

# asset_class values that imply a subtype row should exist. The build plan
# text says "ETF", but ETF is an instrument_type, not an asset_class value
# (the asset_class enum is EQUITY | FUND | BOND | CRYPTO) — FUND covers
# both listed (ETF/ETC/ETN) and non-listed wrappers, distinguished by
# instrument_type, not by a separate asset_class value.
SUBTYPE_TABLE_BY_ASSET_CLASS = {
    "FUND": "instrument_fund",
    "BOND": "instrument_bond",
    "CRYPTO": "instrument_crypto",
}


class SeedValidationError(Exception):
    """One or more dimension-valued fields reference an unknown code.
    Nothing is written to the database when this is raised."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass
class SeedResult:
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        return [f"seeded {count} {table}" for table, count in self.counts.items()]


def _clean(value: Any) -> Any:
    """Blank CSV cells become NULL, never the empty string."""
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def _load_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [{k: _clean(v) for k, v in row.items()} for row in csv.DictReader(f)]


def load_dimensions(path: Path) -> list[dict[str, Any]]:
    return _load_csv(path)


def load_accounts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [{k: _clean(v) for k, v in row.items()} for row in data]


def load_instruments(path: Path) -> list[dict[str, Any]]:
    return _load_csv(path)


def load_aliases(path: Path) -> list[dict[str, Any]]:
    return _load_csv(path)


def load_account_allocations(path: Path) -> list[dict[str, Any]]:
    return _load_csv(path)


def validate_dimension_values(
    dimensions: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    instruments: list[dict[str, Any]],
) -> list[str]:
    known = {(row["dimension"], row["code"]) for row in dimensions}
    errors: list[str] = []

    for i, row in enumerate(accounts):
        identifier = row.get("account_id") or f"row {i + 1}"
        for dim in ACCOUNT_DIMENSION_FIELDS:
            value = row.get(dim)
            if value is not None and (dim, value) not in known:
                errors.append(
                    f"config/accounts.yml: {identifier}: {dim}={value!r} is not a "
                    f"known code (add it to config/dimensions.csv)"
                )

    for i, row in enumerate(instruments):
        identifier = row.get("instrument_id") or f"row {i + 1}"
        for dim in INSTRUMENT_DIMENSION_FIELDS:
            value = row.get(dim)
            if value is not None and (dim, value) not in known:
                errors.append(
                    f"config/instruments.csv: {identifier}: {dim}={value!r} is not a "
                    f"known code (add it to config/dimensions.csv)"
                )

    return errors


def validate_account_allocations(
    accounts: list[dict[str, Any]],
    instruments: list[dict[str, Any]],
    allocations: list[dict[str, Any]],
) -> list[str]:
    """Hard errors, unlike check_subtype_cross_references's warnings: a
    broken account/instrument reference or a snapshot that doesn't sum to
    1 would silently corrupt a general contribution's split, so nothing
    is written until every block is clean. Deliberately stricter than
    core.etp_exposure's uncapped, unsummed weights (build-plan §"look-
    through duality") — that table stores a fund's disclosed, inherently
    partial look-through holdings; this one is your own instruction for
    where 100% of new money goes."""
    account_ids = {row["account_id"] for row in accounts}
    instrument_ids = {row["instrument_id"] for row in instruments}
    errors: list[str] = []

    totals: dict[tuple[str, str], Decimal] = {}
    for i, row in enumerate(allocations):
        identifier = f"row {i + 1}"
        account_id = row.get("account_id")
        instrument_id = row.get("instrument_id")
        as_of_date = row.get("as_of_date")

        if account_id not in account_ids:
            errors.append(
                f"config/account_allocations.csv: {identifier}: "
                f"account_id={account_id!r} is not in config/accounts.yml"
            )
        if instrument_id not in instrument_ids:
            errors.append(
                f"config/account_allocations.csv: {identifier}: "
                f"instrument_id={instrument_id!r} is not in config/instruments.csv"
            )

        try:
            weight = Decimal(str(row.get("weight")))
        except (InvalidOperation, TypeError):
            errors.append(
                f"config/account_allocations.csv: {identifier}: "
                f"weight={row.get('weight')!r} is not a number"
            )
            continue
        if not (Decimal("0") < weight <= Decimal("1")):
            errors.append(
                f"config/account_allocations.csv: {identifier}: "
                f"weight={weight} must be greater than 0 and at most 1"
            )
        key = (account_id, as_of_date)
        totals[key] = totals.get(key, Decimal("0")) + weight

    for (account_id, as_of_date), total in totals.items():
        if abs(total - Decimal("1")) > Decimal("0.001"):
            errors.append(
                f"config/account_allocations.csv: {account_id} as of "
                f"{as_of_date}: weights sum to {total}, not 1"
            )

    return errors


def check_subtype_cross_references(
    instruments: list[dict[str, Any]],
    fund_rows: list[dict[str, Any]],
    bond_rows: list[dict[str, Any]],
    crypto_rows: list[dict[str, Any]],
) -> list[str]:
    """Both directions are warnings, never blocks."""
    warnings: list[str] = []

    instrument_ids = {row["instrument_id"] for row in instruments}
    subtype_ids_by_table = {
        "instrument_fund": {row["instrument_id"] for row in fund_rows},
        "instrument_bond": {row["instrument_id"] for row in bond_rows},
        "instrument_crypto": {row["instrument_id"] for row in crypto_rows},
    }

    for row in instruments:
        table = SUBTYPE_TABLE_BY_ASSET_CLASS.get(row.get("asset_class"))
        if table and row["instrument_id"] not in subtype_ids_by_table[table]:
            warnings.append(
                f"{row['instrument_id']}: asset_class={row['asset_class']!r} expects a "
                f"row in config/instruments_{table.removeprefix('instrument_')}.csv, none found"
            )

    for _table, rows, filename in (
        ("instrument_fund", fund_rows, "instruments_fund.csv"),
        ("instrument_bond", bond_rows, "instruments_bond.csv"),
        ("instrument_crypto", crypto_rows, "instruments_crypto.csv"),
    ):
        for row in rows:
            if row["instrument_id"] not in instrument_ids:
                warnings.append(
                    f"config/{filename}: instrument_id={row['instrument_id']!r} is not "
                    f"in config/instruments.csv"
                )

    return warnings


def _upsert_many(
    cur: psycopg.Cursor,
    rows: list[dict[str, Any]],
    sql: str,
    columns: tuple[str, ...],
) -> None:
    # psycopg requires every %(name)s placeholder to have a matching key —
    # a config row that simply omits an optional column would otherwise
    # raise "query parameter missing". Fill those in as NULL explicitly.
    for row in rows:
        cur.execute(sql, {col: row.get(col) for col in columns})


_DIMENSION_COLUMNS = ("dimension", "code", "label_en", "sort_order")

_ACCOUNT_COLUMNS = (
    "account_id", "broker", "ultimate_parent", "custodian", "account_type",
    "fiscal_envelope", "custody_type", "holding_structure", "key_storage",
    "contractual_rate", "maturity_date", "deposit_ceiling", "guarantee_scheme",
    "base_currency", "opened_on", "closed_on", "is_active",
)

_INSTRUMENT_COLUMNS = (
    "instrument_id", "isin", "yf_symbol", "ticker", "name", "asset_class",
    "instrument_type", "currency", "sector", "industry", "description", "issuer",
    "domicile_country", "protection_type", "is_pea_eligible", "price_source",
    "is_active",
)

_INSTRUMENT_FUND_COLUMNS = (
    "instrument_id", "is_ucits", "rhp_years",
    "distribution_policy", "ongoing_charges", "sri", "replication_method",
    "swap_counterparty", "uses_sec_lending", "custodian", "sfdr_article",
    "benchmark_index", "justetf_id", "subscription_price", "withdrawal_price",
    "management_company", "property_sector", "occupancy_rate", "distribution_rate",
    "investment_focus", "fund_size", "investment_approach", "sustainability",
    "currency_risk", "fund_currency", "volatility_1y_eur", "inception_date",
    "distribution_frequency",
)

_INSTRUMENT_BOND_COLUMNS = (
    "instrument_id", "coupon_rate", "coupon_frequency", "maturity_date",
    "face_value", "issuer_type", "credit_rating", "seniority", "is_callable",
)

_INSTRUMENT_CRYPTO_COLUMNS = (
    "instrument_id", "chain", "contract_address", "token_standard", "consensus",
    "is_stablecoin", "peg_currency",
)

_ALIAS_COLUMNS = ("alias", "source", "instrument_id")

_ACCOUNT_ALLOCATION_COLUMNS = ("account_id", "as_of_date", "instrument_id", "weight", "note")


_DIMENSION_SQL = """
    INSERT INTO core.dimensions (dimension, code, label_en, sort_order)
    VALUES (%(dimension)s, %(code)s, %(label_en)s, COALESCE(%(sort_order)s, 0))
    ON CONFLICT (dimension, code) DO UPDATE SET
        label_en = EXCLUDED.label_en,
        sort_order = EXCLUDED.sort_order
"""

_ACCOUNT_SQL = """
    INSERT INTO core.accounts (
        account_id, broker, ultimate_parent, custodian, account_type,
        fiscal_envelope, custody_type, holding_structure, key_storage,
        contractual_rate, maturity_date, deposit_ceiling, guarantee_scheme,
        base_currency, opened_on, closed_on, is_active
    ) VALUES (
        %(account_id)s, %(broker)s, %(ultimate_parent)s, %(custodian)s,
        %(account_type)s, %(fiscal_envelope)s, %(custody_type)s,
        COALESCE(%(holding_structure)s, 'UNKNOWN'), %(key_storage)s,
        %(contractual_rate)s, %(maturity_date)s, %(deposit_ceiling)s,
        %(guarantee_scheme)s, %(base_currency)s, %(opened_on)s, %(closed_on)s,
        COALESCE(%(is_active)s, true)
    )
    ON CONFLICT (account_id) DO UPDATE SET
        broker = EXCLUDED.broker,
        ultimate_parent = EXCLUDED.ultimate_parent,
        custodian = EXCLUDED.custodian,
        account_type = EXCLUDED.account_type,
        fiscal_envelope = EXCLUDED.fiscal_envelope,
        custody_type = EXCLUDED.custody_type,
        holding_structure = EXCLUDED.holding_structure,
        key_storage = EXCLUDED.key_storage,
        contractual_rate = EXCLUDED.contractual_rate,
        maturity_date = EXCLUDED.maturity_date,
        deposit_ceiling = EXCLUDED.deposit_ceiling,
        guarantee_scheme = EXCLUDED.guarantee_scheme,
        base_currency = EXCLUDED.base_currency,
        opened_on = EXCLUDED.opened_on,
        closed_on = EXCLUDED.closed_on,
        is_active = EXCLUDED.is_active
"""

_INSTRUMENT_SQL = """
    INSERT INTO core.instruments (
        instrument_id, isin, yf_symbol, ticker, name, asset_class, instrument_type,
        currency, sector, industry, description, issuer, domicile_country,
        protection_type, is_pea_eligible, price_source, is_active
    ) VALUES (
        %(instrument_id)s, %(isin)s, %(yf_symbol)s, %(ticker)s, %(name)s,
        %(asset_class)s, %(instrument_type)s, %(currency)s, %(sector)s,
        %(industry)s, %(description)s, %(issuer)s, %(domicile_country)s,
        %(protection_type)s, %(is_pea_eligible)s,
        COALESCE(%(price_source)s, 'yfinance'), COALESCE(%(is_active)s, true)
    )
    ON CONFLICT (instrument_id) DO UPDATE SET
        isin = EXCLUDED.isin,
        yf_symbol = EXCLUDED.yf_symbol,
        ticker = EXCLUDED.ticker,
        name = EXCLUDED.name,
        asset_class = EXCLUDED.asset_class,
        instrument_type = EXCLUDED.instrument_type,
        currency = EXCLUDED.currency,
        sector = EXCLUDED.sector,
        industry = EXCLUDED.industry,
        description = EXCLUDED.description,
        issuer = EXCLUDED.issuer,
        domicile_country = EXCLUDED.domicile_country,
        protection_type = EXCLUDED.protection_type,
        is_pea_eligible = EXCLUDED.is_pea_eligible,
        price_source = EXCLUDED.price_source,
        is_active = EXCLUDED.is_active
"""

_INSTRUMENT_FUND_SQL = """
    INSERT INTO core.instrument_fund (
        instrument_id, is_ucits, rhp_years,
        distribution_policy, ongoing_charges, sri, replication_method,
        swap_counterparty, uses_sec_lending, custodian, sfdr_article,
        benchmark_index, justetf_id, subscription_price, withdrawal_price,
        management_company, property_sector, occupancy_rate, distribution_rate,
        investment_focus, fund_size, investment_approach, sustainability,
        currency_risk, fund_currency, volatility_1y_eur, inception_date,
        distribution_frequency
    ) VALUES (
        %(instrument_id)s, %(is_ucits)s, %(rhp_years)s,
        %(distribution_policy)s, %(ongoing_charges)s, %(sri)s,
        %(replication_method)s, %(swap_counterparty)s, %(uses_sec_lending)s,
        %(custodian)s, %(sfdr_article)s, %(benchmark_index)s, %(justetf_id)s,
        %(subscription_price)s, %(withdrawal_price)s, %(management_company)s,
        %(property_sector)s, %(occupancy_rate)s, %(distribution_rate)s,
        %(investment_focus)s, %(fund_size)s, %(investment_approach)s,
        %(sustainability)s, %(currency_risk)s, %(fund_currency)s,
        %(volatility_1y_eur)s, %(inception_date)s, %(distribution_frequency)s
    )
    ON CONFLICT (instrument_id) DO UPDATE SET
        is_ucits = EXCLUDED.is_ucits,
        rhp_years = EXCLUDED.rhp_years,
        distribution_policy = EXCLUDED.distribution_policy,
        ongoing_charges = EXCLUDED.ongoing_charges,
        sri = EXCLUDED.sri,
        replication_method = EXCLUDED.replication_method,
        swap_counterparty = EXCLUDED.swap_counterparty,
        uses_sec_lending = EXCLUDED.uses_sec_lending,
        custodian = EXCLUDED.custodian,
        sfdr_article = EXCLUDED.sfdr_article,
        benchmark_index = EXCLUDED.benchmark_index,
        justetf_id = EXCLUDED.justetf_id,
        subscription_price = EXCLUDED.subscription_price,
        withdrawal_price = EXCLUDED.withdrawal_price,
        management_company = EXCLUDED.management_company,
        property_sector = EXCLUDED.property_sector,
        occupancy_rate = EXCLUDED.occupancy_rate,
        distribution_rate = EXCLUDED.distribution_rate,
        investment_focus = EXCLUDED.investment_focus,
        fund_size = EXCLUDED.fund_size,
        investment_approach = EXCLUDED.investment_approach,
        sustainability = EXCLUDED.sustainability,
        currency_risk = EXCLUDED.currency_risk,
        fund_currency = EXCLUDED.fund_currency,
        volatility_1y_eur = EXCLUDED.volatility_1y_eur,
        inception_date = EXCLUDED.inception_date,
        distribution_frequency = EXCLUDED.distribution_frequency
"""

_INSTRUMENT_BOND_SQL = """
    INSERT INTO core.instrument_bond (
        instrument_id, coupon_rate, coupon_frequency, maturity_date,
        face_value, issuer_type, credit_rating, seniority, is_callable
    ) VALUES (
        %(instrument_id)s, %(coupon_rate)s, %(coupon_frequency)s,
        %(maturity_date)s, %(face_value)s, %(issuer_type)s, %(credit_rating)s,
        %(seniority)s, COALESCE(%(is_callable)s, false)
    )
    ON CONFLICT (instrument_id) DO UPDATE SET
        coupon_rate = EXCLUDED.coupon_rate,
        coupon_frequency = EXCLUDED.coupon_frequency,
        maturity_date = EXCLUDED.maturity_date,
        face_value = EXCLUDED.face_value,
        issuer_type = EXCLUDED.issuer_type,
        credit_rating = EXCLUDED.credit_rating,
        seniority = EXCLUDED.seniority,
        is_callable = EXCLUDED.is_callable
"""

_INSTRUMENT_CRYPTO_SQL = """
    INSERT INTO core.instrument_crypto (
        instrument_id, chain, contract_address, token_standard, consensus,
        is_stablecoin, peg_currency
    ) VALUES (
        %(instrument_id)s, %(chain)s, %(contract_address)s, %(token_standard)s,
        %(consensus)s, COALESCE(%(is_stablecoin)s, false), %(peg_currency)s
    )
    ON CONFLICT (instrument_id) DO UPDATE SET
        chain = EXCLUDED.chain,
        contract_address = EXCLUDED.contract_address,
        token_standard = EXCLUDED.token_standard,
        consensus = EXCLUDED.consensus,
        is_stablecoin = EXCLUDED.is_stablecoin,
        peg_currency = EXCLUDED.peg_currency
"""

_ALIAS_SQL = """
    INSERT INTO core.instrument_aliases (alias, source, instrument_id)
    VALUES (%(alias)s, COALESCE(%(source)s, 'manual'), %(instrument_id)s)
    ON CONFLICT (alias, source) DO UPDATE SET
        instrument_id = EXCLUDED.instrument_id
"""

_ACCOUNT_ALLOCATION_SQL = """
    INSERT INTO core.account_target_allocations (
        account_id, as_of_date, instrument_id, weight, note
    ) VALUES (
        %(account_id)s, %(as_of_date)s, %(instrument_id)s, %(weight)s, %(note)s
    )
    ON CONFLICT (account_id, as_of_date, instrument_id) DO UPDATE SET
        weight = EXCLUDED.weight,
        note = EXCLUDED.note
"""


def seed(conn: psycopg.Connection, config_dir: Path | None = None) -> SeedResult:
    # Resolved at call time, not import time, so tests can monkeypatch
    # seed.CONFIG_DIR and have it take effect.
    config_dir = config_dir if config_dir is not None else CONFIG_DIR

    dimensions = load_dimensions(config_dir / "dimensions.csv")
    accounts = load_accounts(config_dir / "accounts.yml")
    instruments = load_instruments(config_dir / "instruments.csv")
    fund_rows = _load_csv(config_dir / "instruments_fund.csv")
    bond_rows = _load_csv(config_dir / "instruments_bond.csv")
    crypto_rows = _load_csv(config_dir / "instruments_crypto.csv")
    aliases = load_aliases(config_dir / "aliases.csv")
    account_allocations = load_account_allocations(config_dir / "account_allocations.csv")

    errors = validate_dimension_values(dimensions, accounts, instruments)
    errors += validate_account_allocations(accounts, instruments, account_allocations)
    if errors:
        raise SeedValidationError(errors)

    warnings = check_subtype_cross_references(
        instruments, fund_rows, bond_rows, crypto_rows
    )

    with conn.cursor() as cur:
        _upsert_many(cur, dimensions, _DIMENSION_SQL, _DIMENSION_COLUMNS)
        _upsert_many(cur, accounts, _ACCOUNT_SQL, _ACCOUNT_COLUMNS)
        _upsert_many(cur, instruments, _INSTRUMENT_SQL, _INSTRUMENT_COLUMNS)
        _upsert_many(cur, fund_rows, _INSTRUMENT_FUND_SQL, _INSTRUMENT_FUND_COLUMNS)
        _upsert_many(cur, bond_rows, _INSTRUMENT_BOND_SQL, _INSTRUMENT_BOND_COLUMNS)
        _upsert_many(
            cur, crypto_rows, _INSTRUMENT_CRYPTO_SQL, _INSTRUMENT_CRYPTO_COLUMNS
        )
        _upsert_many(cur, aliases, _ALIAS_SQL, _ALIAS_COLUMNS)
        _upsert_many(
            cur, account_allocations, _ACCOUNT_ALLOCATION_SQL, _ACCOUNT_ALLOCATION_COLUMNS
        )
    conn.commit()

    return SeedResult(
        counts={
            "dimensions": len(dimensions),
            "accounts": len(accounts),
            "instruments": len(instruments),
            "instrument_fund rows": len(fund_rows),
            "instrument_bond rows": len(bond_rows),
            "instrument_crypto rows": len(crypto_rows),
            "aliases": len(aliases),
            "account_allocations": len(account_allocations),
        },
        warnings=warnings,
    )
