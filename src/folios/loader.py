from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import psycopg

from folios import allocations, fx, seed, validate
from folios.models import (
    EntryRow,
    compute_net_amount,
    compute_signed_quantity,
    effective_gross,
)

REPO_ROOT = validate.REPO_ROOT
DEFAULT_DATA_DIR = REPO_ROOT / "data" / "manual"

# Columns compared between the existing DB row and the newly computed one
# to report exactly which fields changed on an update. Deliberately
# excludes entry_id/txn_hash/created_at/source*/fx_rate_source — those
# either can't change (entry_id) or aren't user-meaningful edits.
_COMPARE_COLUMNS = (
    "trade_date", "txn_type", "instrument_id", "quantity", "price",
    "gross_amount", "fee", "tax", "net_amount", "currency", "note",
)


class LoadValidationError(Exception):
    """folios load refuses to insert anything if any row fails validation."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass
class LoadResult:
    rows_read: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_skipped: int = 0
    updated_fields: dict[str, list[str]] = field(default_factory=dict)


def _existing_rows(
    conn: psycopg.Connection, entry_ids: list[str]
) -> dict[str, dict[str, object]]:
    if not entry_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT entry_id, txn_hash, "
            + ", ".join(_COMPARE_COLUMNS)
            + " FROM core.transactions WHERE entry_id = ANY(%s)",
            (entry_ids,),
        )
        columns = [d.name for d in cur.description]
        return {row[0]: dict(zip(columns, row, strict=True)) for row in cur.fetchall()}


_UPSERT_SQL = """
    INSERT INTO core.transactions (
        entry_id, txn_hash, account_id, trade_date, txn_type, instrument_id,
        quantity, price, gross_amount, fee, tax, net_amount, currency,
        fx_rate_to_base, fx_rate_date, fx_rate_source, note, source, source_file
    ) VALUES (
        %(entry_id)s, %(txn_hash)s, %(account_id)s, %(trade_date)s, %(txn_type)s,
        %(instrument_id)s, %(quantity)s, %(price)s, %(gross_amount)s, %(fee)s,
        %(tax)s, %(net_amount)s, %(currency)s, %(fx_rate_to_base)s,
        %(fx_rate_date)s, 'ecb', %(note)s, 'manual', %(source_file)s
    )
    ON CONFLICT (entry_id) DO UPDATE SET
        txn_hash = EXCLUDED.txn_hash,
        account_id = EXCLUDED.account_id,
        trade_date = EXCLUDED.trade_date,
        txn_type = EXCLUDED.txn_type,
        instrument_id = EXCLUDED.instrument_id,
        quantity = EXCLUDED.quantity,
        price = EXCLUDED.price,
        gross_amount = EXCLUDED.gross_amount,
        fee = EXCLUDED.fee,
        tax = EXCLUDED.tax,
        net_amount = EXCLUDED.net_amount,
        currency = EXCLUDED.currency,
        fx_rate_to_base = EXCLUDED.fx_rate_to_base,
        fx_rate_date = EXCLUDED.fx_rate_date,
        note = EXCLUDED.note,
        source_file = EXCLUDED.source_file
"""

_LOAD_RUN_SQL = """
    INSERT INTO core.load_runs
        (source, source_file, file_sha256, rows_read, rows_inserted,
         rows_updated, rows_skipped, status, message, started_at, finished_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _record_load_run(
    conn: psycopg.Connection,
    path: Path,
    result: LoadResult,
    status: str,
    message: str | None,
    started_at: datetime,
    finished_at: datetime | None,
) -> None:
    file_sha256 = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    with conn.cursor() as cur:
        cur.execute(
            _LOAD_RUN_SQL,
            (
                "manual", validate.relative_path(path), file_sha256, result.rows_read,
                result.rows_inserted, result.rows_updated, result.rows_skipped,
                status, message, started_at, finished_at,
            ),
        )
    conn.commit()


def _plan_rows(
    conn: psycopg.Connection,
    parsed: list[tuple[int, str, str, EntryRow]],
    aliases: dict[str, str],
) -> list[dict[str, object]]:
    """One dict per eventual core.transactions row. A normal row plans to
    exactly one; a general contribution (a BUY with no symbol) plans to
    one per instrument in the account's current target allocation,
    computed by allocations.expand_contribution — each gets its own
    entry_id (csv:<path>:<line>#<instrument_id>), still derived from the
    same physical CSV line, never a content hash, so it stays a stable
    key across reloads. validate_parsed already confirmed every target
    instrument has a price, so expand_contribution isn't expected to
    raise here — a fresh MissingPriceError would only mean prices
    changed between validate and load, which can't happen mid-run."""
    plans: list[dict[str, object]] = []
    for _line, entry_id, txn_hash, row in parsed:
        if row.type == "BUY" and row.symbol is None:
            for expansion in allocations.expand_contribution(
                conn, row.account, row.entry_date, effective_gross(row)
            ):
                plans.append(
                    {
                        "entry_id": f"{entry_id}#{expansion['instrument_id']}",
                        "txn_hash": txn_hash,
                        "account_id": row.account,
                        "trade_date": row.entry_date,
                        "txn_type": row.type,
                        "instrument_id": expansion["instrument_id"],
                        "quantity": expansion["quantity"],
                        "price": expansion["price"],
                        "gross_amount": expansion["gross_amount"],
                        "fee": Decimal("0"),
                        "tax": Decimal("0"),
                        "net_amount": expansion["net_amount"],
                        "currency": row.currency,
                        "note": row.note,
                    }
                )
        else:
            instrument_id = aliases.get(row.symbol) if row.symbol else None
            plans.append(
                {
                    "entry_id": entry_id,
                    "txn_hash": txn_hash,
                    "account_id": row.account,
                    "trade_date": row.entry_date,
                    "txn_type": row.type,
                    "instrument_id": instrument_id,
                    "quantity": compute_signed_quantity(row),
                    "price": row.price,
                    "gross_amount": effective_gross(row),
                    "fee": row.fee,
                    "tax": row.tax,
                    "net_amount": compute_net_amount(row),
                    "currency": row.currency,
                    "note": row.note,
                }
            )
    return plans


def load_file(conn: psycopg.Connection, path: Path) -> LoadResult:
    """Validate, then upsert every row via ON CONFLICT (entry_id) DO
    UPDATE. Refuses to insert anything — and records a failed load_runs
    row — if any row fails validation."""
    started_at = datetime.now(UTC)
    parsed, parse_errors = validate.parse_rows(path)
    messages = parse_errors + validate.validate_parsed(conn, parsed)
    errors = [m for m in messages if m.level == "error"]

    result = LoadResult(rows_read=len(validate.read_rows(path)))

    if errors:
        _record_load_run(
            conn, path, result, status="error",
            message="; ".join(str(e) for e in errors),
            started_at=started_at, finished_at=datetime.now(UTC),
        )
        raise LoadValidationError([str(e) for e in errors])

    aliases = validate.alias_to_instrument(conn)
    plans = _plan_rows(conn, parsed, aliases)
    existing = _existing_rows(conn, [plan["entry_id"] for plan in plans])
    relative = validate.relative_path(path)

    for plan in plans:
        entry_id = plan["entry_id"]
        txn_hash = plan["txn_hash"]
        prior = existing.get(entry_id)
        if prior is not None and prior["txn_hash"] == txn_hash:
            result.rows_skipped += 1
            continue

        if plan["currency"] == "EUR":
            fx_rate: Decimal = Decimal("1")
            fx_rate_date = plan["trade_date"]
        else:
            fx_rate, fx_rate_date = fx.resolve_rate_with_date(
                conn, plan["trade_date"], "EUR", plan["currency"]
            )

        params = {
            **plan,
            "fx_rate_to_base": fx_rate,
            "fx_rate_date": fx_rate_date,
            "source_file": relative,
        }

        with conn.cursor() as cur:
            cur.execute(_UPSERT_SQL, params)

        if prior is None:
            result.rows_inserted += 1
        else:
            changed = [col for col in _COMPARE_COLUMNS if prior.get(col) != params.get(col)]
            result.rows_updated += 1
            if changed:
                result.updated_fields[entry_id] = changed

    conn.commit()
    _record_load_run(
        conn, path, result, status="success", message=None,
        started_at=started_at, finished_at=datetime.now(UTC),
    )
    return result


def _transaction_files(data_dir: Path) -> list[Path]:
    """Every transaction-shaped CSV in data_dir — the curated
    transactions.csv plus every gform_<date>.csv `folios pull` has
    written. Filenames containing "valuations" (valuations.csv,
    gform_valuations_<date>.csv) live in the same directory but have a
    different column shape entirely — see valuations.load_all."""
    return sorted(p for p in data_dir.glob("*.csv") if "valuations" not in p.name)


def load_all(
    conn: psycopg.Connection, data_dir: Path | None = None
) -> tuple[LoadResult, list[str]]:
    """Loads every transaction file in data_dir, aggregating the result.
    Unlike load_file's own all-or-nothing guarantee (which this still
    honours per file), one bad file's errors don't block the others —
    used by `folios sync`, where a broken phone submission shouldn't
    also stop today's market-data refresh. Returns (aggregate result,
    per-file error messages)."""
    data_dir = data_dir if data_dir is not None else DEFAULT_DATA_DIR
    total = LoadResult()
    errors: list[str] = []
    if not data_dir.exists():
        return total, errors

    for path in _transaction_files(data_dir):
        try:
            result = load_file(conn, path)
        except LoadValidationError as exc:
            errors.extend(str(e) for e in exc.errors)
            continue
        total.rows_read += result.rows_read
        total.rows_inserted += result.rows_inserted
        total.rows_updated += result.rows_updated
        total.rows_skipped += result.rows_skipped
        total.updated_fields.update(result.updated_fields)
    return total, errors


def rebuild(
    conn: psycopg.Connection,
    data_dir: Path | None = None,
    config_dir: Path | None = None,
) -> LoadResult:
    """folios rebuild — reproduces core.transactions and the reference
    tables from files alone. Never touches core.fx_rates or core.prices:
    those come from external APIs, not entry files, so they're outside
    what "rebuild from files" means. Not byte-for-byte: created_at
    defaults to now(), so a rebuilt row's timestamp is fresh.

    core.prices has an FK on instrument_id, so TRUNCATE ... CASCADE on
    core.instruments would otherwise silently wipe it too (Postgres
    cascades TRUNCATE to any table referencing a truncated one, listed or
    not) — losing irreplaceable price_source=manual valuation history
    that no external API can refetch. Backed up and restored around the
    truncate instead; a price row for an instrument since removed from
    config isn't restored, same as it would have been lost before."""
    data_dir = data_dir if data_dir is not None else DEFAULT_DATA_DIR

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS _rebuild_prices_backup")
        cur.execute("CREATE TEMP TABLE _rebuild_prices_backup AS SELECT * FROM core.prices")
        cur.execute(
            "TRUNCATE core.dimensions, core.accounts, core.instruments, "
            "core.instrument_fund, core.instrument_bond, core.instrument_crypto, "
            "core.instrument_aliases, core.account_target_allocations, "
            "core.transactions CASCADE"
        )
    conn.commit()

    seed.seed(conn, config_dir)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.prices SELECT * FROM _rebuild_prices_backup "
            "WHERE instrument_id IN (SELECT instrument_id FROM core.instruments)"
        )
        cur.execute("DROP TABLE _rebuild_prices_backup")
    conn.commit()

    total = LoadResult()
    if data_dir.exists():
        for path in _transaction_files(data_dir):
            result = load_file(conn, path)
            total.rows_read += result.rows_read
            total.rows_inserted += result.rows_inserted
            total.rows_updated += result.rows_updated
            total.rows_skipped += result.rows_skipped
            total.updated_fields.update(result.updated_fields)
    return total
