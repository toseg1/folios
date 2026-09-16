from __future__ import annotations

import csv
import difflib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from pydantic import ValidationError

from folios import fx
from folios.models import EntryRow, build_entry_id, compute_txn_hash

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENTRIES_PATH = REPO_ROOT / "data" / "manual" / "transactions.csv"

# Quantity-affecting types, signed the way core.transactions stores them.
# The CSV itself only ever holds positive magnitudes (build-plan §4); this
# is what turns "SELL 5" into "-5" for the running-position check.
_QUANTITY_SIGN = {
    "BUY": 1,
    "SELL": -1,
    "TRANSFER_IN": 1,
    "TRANSFER_OUT": -1,
    "OPENING_BALANCE": 1,
    "STAKING": 1,
    # SPLIT's CSV shape isn't specified by the build plan (the Form takes
    # "new total quantity", the loader computes the delta) — treated
    # additively here as a known simplification; not yet correct for a
    # reverse split entered directly via CSV.
    "SPLIT": 1,
}


@dataclass
class Message:
    level: str  # "error" | "warning" | "info"
    line: int
    entry_id: str
    text: str

    def __str__(self) -> str:
        return f"{self.level.upper()} line {self.line} ({self.entry_id}): {self.text}"


def read_rows(path: Path) -> list[tuple[int, dict[str, Any]]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        # The header is line 1, so the first data row is line 2.
        return [(i + 2, row) for i, row in enumerate(reader)]


def _relative_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _parse_rows(
    path: Path,
) -> tuple[list[tuple[int, str, str, EntryRow]], list[Message]]:
    relative = _relative_path(path)
    parsed: list[tuple[int, str, str, EntryRow]] = []
    messages: list[Message] = []

    for line, raw in read_rows(path):
        entry_id = build_entry_id(relative, line)
        txn_hash = compute_txn_hash(raw)
        try:
            row = EntryRow(
                entry_date=raw.get("date"),
                account=raw.get("account"),
                type=raw.get("type"),
                symbol=raw.get("symbol"),
                quantity=raw.get("quantity"),
                price=raw.get("price"),
                gross=raw.get("gross"),
                fee=raw.get("fee"),
                tax=raw.get("tax"),
                currency=raw.get("currency"),
                note=raw.get("note"),
            )
        except ValidationError as exc:
            for error in exc.errors():
                messages.append(Message("error", line, entry_id, error["msg"]))
            continue
        parsed.append((line, entry_id, txn_hash, row))

    return parsed, messages


def _known_accounts(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT account_id FROM core.accounts")
        return {row[0] for row in cur.fetchall()}


def _alias_to_instrument(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT alias, instrument_id FROM core.instrument_aliases")
        return dict(cur.fetchall())


def _instrument_currencies(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT instrument_id, currency FROM core.instruments")
        return dict(cur.fetchall())


def _existing_position(
    conn: psycopg.Connection,
    account: str,
    instrument_id: str,
    exclude_entry_ids: set[str],
) -> Decimal:
    """Signed quantity already in core.transactions for this account and
    instrument — rows already loaded from a previous pass, never the file
    being validated now (re-validating an already-loaded file must not
    double count it)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT entry_id, quantity FROM core.transactions
            WHERE account_id = %s AND instrument_id = %s
              AND txn_type = ANY(%s) AND quantity IS NOT NULL
            """,
            (account, instrument_id, list(_QUANTITY_SIGN)),
        )
        rows = cur.fetchall()
    return sum(
        (qty for entry_id, qty in rows if entry_id not in exclude_entry_ids),
        Decimal("0"),
    )


def validate_file(conn: psycopg.Connection, path: Path) -> list[Message]:
    """Every rule in build-plan §4. A dry run: never writes to
    core.transactions, only reads reference data to check against."""
    parsed, messages = _parse_rows(path)

    accounts = _known_accounts(conn)
    aliases = _alias_to_instrument(conn)
    alias_names = sorted(aliases)
    instrument_currencies = _instrument_currencies(conn)
    file_entry_ids = {entry_id for _, entry_id, _, _ in parsed}

    for line, entry_id, _txn_hash, row in parsed:
        if row.account not in accounts:
            messages.append(
                Message(
                    "error", line, entry_id,
                    f"account={row.account!r} does not exist in core.accounts",
                )
            )

    for line, entry_id, _txn_hash, row in parsed:
        if row.symbol is not None and row.symbol not in aliases:
            suggestions = difflib.get_close_matches(row.symbol, alias_names, n=3)
            hint = f" (did you mean: {', '.join(suggestions)}?)" if suggestions else ""
            messages.append(
                Message(
                    "error", line, entry_id,
                    f"symbol={row.symbol!r} does not resolve via "
                    f"instrument_aliases{hint}",
                )
            )

    for line, entry_id, _txn_hash, row in parsed:
        if row.currency != "EUR":
            try:
                fx.resolve_rate(conn, row.entry_date, "EUR", row.currency)
            except fx.FxRateNotFoundError:
                messages.append(
                    Message(
                        "error", line, entry_id,
                        f"no FX rate covers {row.currency} on "
                        f"{row.entry_date.isoformat()} "
                        f"(run `folios fx --since ...` first)",
                    )
                )

    for line, entry_id, _txn_hash, row in parsed:
        if row.symbol is None:
            continue
        instrument_id = aliases.get(row.symbol)
        expected_currency = (
            instrument_currencies.get(instrument_id) if instrument_id else None
        )
        if expected_currency and row.currency != expected_currency:
            messages.append(
                Message(
                    "warning", line, entry_id,
                    f"currency={row.currency!r} does not match "
                    f"{row.symbol}'s instrument currency "
                    f"({expected_currency!r})",
                )
            )

    positions: dict[tuple[str, str], Decimal] = {}
    for line, entry_id, _txn_hash, row in sorted(
        parsed, key=lambda p: (p[3].entry_date, p[0])
    ):
        if row.symbol is None or row.type not in _QUANTITY_SIGN or row.quantity is None:
            continue
        instrument_id = aliases.get(row.symbol)
        if instrument_id is None:
            continue  # already reported above as an unresolved symbol
        key = (row.account, instrument_id)
        if key not in positions:
            positions[key] = _existing_position(
                conn, row.account, instrument_id, file_entry_ids
            )
        positions[key] += _QUANTITY_SIGN[row.type] * row.quantity
        if row.type == "SELL" and positions[key] < 0:
            messages.append(
                Message(
                    "error", line, entry_id,
                    f"SELL drives the position for {row.symbol} in "
                    f"{row.account} negative ({positions[key]})",
                )
            )

    if file_entry_ids:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT entry_id, txn_hash FROM core.transactions "
                "WHERE entry_id = ANY(%s)",
                (list(file_entry_ids),),
            )
            existing_hashes = dict(cur.fetchall())
        for line, entry_id, txn_hash, _row in parsed:
            existing = existing_hashes.get(entry_id)
            if existing is None:
                continue
            if existing == txn_hash:
                messages.append(
                    Message("info", line, entry_id, "unchanged, will be skipped")
                )
            else:
                messages.append(
                    Message("info", line, entry_id, "changed, will be updated")
                )

    return messages
