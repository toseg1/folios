from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import psycopg

from folios import fx, seed, validate
from folios.models import compute_net_amount, compute_signed_quantity, effective_gross

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
    entry_ids = [entry_id for _, entry_id, _, _ in parsed]
    existing = _existing_rows(conn, entry_ids)
    relative = validate.relative_path(path)

    for _line, entry_id, txn_hash, row in parsed:
        prior = existing.get(entry_id)
        if prior is not None and prior["txn_hash"] == txn_hash:
            result.rows_skipped += 1
            continue

        instrument_id = aliases.get(row.symbol) if row.symbol else None
        gross = effective_gross(row)
        net_amount = compute_net_amount(row)
        quantity = compute_signed_quantity(row)

        if row.currency == "EUR":
            fx_rate: Decimal = Decimal("1")
            fx_rate_date = row.entry_date
        else:
            fx_rate, fx_rate_date = fx.resolve_rate_with_date(
                conn, row.entry_date, "EUR", row.currency
            )

        params = {
            "entry_id": entry_id,
            "txn_hash": txn_hash,
            "account_id": row.account,
            "trade_date": row.entry_date,
            "txn_type": row.type,
            "instrument_id": instrument_id,
            "quantity": quantity,
            "price": row.price,
            "gross_amount": gross,
            "fee": row.fee,
            "tax": row.tax,
            "net_amount": net_amount,
            "currency": row.currency,
            "fx_rate_to_base": fx_rate,
            "fx_rate_date": fx_rate_date,
            "note": row.note,
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


def rebuild(
    conn: psycopg.Connection,
    data_dir: Path | None = None,
    config_dir: Path | None = None,
) -> LoadResult:
    """folios rebuild — reproduces core.transactions and the reference
    tables from files alone. Never touches core.fx_rates or core.prices:
    those come from external APIs, not entry files, so they're outside
    what "rebuild from files" means. Not byte-for-byte: created_at
    defaults to now(), so a rebuilt row's timestamp is fresh."""
    data_dir = data_dir if data_dir is not None else DEFAULT_DATA_DIR

    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE core.dimensions, core.accounts, core.instruments, "
            "core.instrument_fund, core.instrument_bond, core.instrument_crypto, "
            "core.instrument_aliases, core.transactions CASCADE"
        )
    conn.commit()

    seed.seed(conn, config_dir)

    total = LoadResult()
    if data_dir.exists():
        for path in sorted(data_dir.glob("*.csv")):
            result = load_file(conn, path)
            total.rows_read += result.rows_read
            total.rows_inserted += result.rows_inserted
            total.rows_updated += result.rows_updated
            total.rows_skipped += result.rows_skipped
            total.updated_fields.update(result.updated_fields)
    return total
