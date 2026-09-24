from __future__ import annotations

import csv
import difflib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from pydantic import ValidationError

from folios import allocations, fx
from folios.models import QUANTITY_SIGN, EntryRow, build_entry_id, compute_txn_hash

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENTRIES_PATH = REPO_ROOT / "data" / "manual" / "transactions.csv"


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


def relative_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def parse_rows(
    path: Path,
) -> tuple[list[tuple[int, str, str, EntryRow]], list[Message]]:
    """Parse every row through EntryRow. Returns (parsed rows, messages for
    rows that failed to parse) — used by both `folios validate` and
    `folios load`, so the two never disagree on what counts as valid."""
    relative = relative_path(path)
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


def known_accounts(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT account_id FROM core.accounts")
        return {row[0] for row in cur.fetchall()}


def alias_to_instrument(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT alias, instrument_id FROM core.instrument_aliases")
        return dict(cur.fetchall())


def instrument_currencies(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT instrument_id, currency FROM core.instruments")
        return dict(cur.fetchall())


def existing_txn_hashes(
    conn: psycopg.Connection, entry_ids: set[str] | list[str]
) -> dict[str, str]:
    entry_ids = list(entry_ids)
    if not entry_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT entry_id, txn_hash FROM core.transactions WHERE entry_id = ANY(%s)",
            (entry_ids,),
        )
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
            (account, instrument_id, list(QUANTITY_SIGN)),
        )
        rows = cur.fetchall()
    return sum(
        (qty for entry_id, qty in rows if entry_id not in exclude_entry_ids),
        Decimal("0"),
    )


def validate_parsed(
    conn: psycopg.Connection, parsed: list[tuple[int, str, str, EntryRow]]
) -> list[Message]:
    """The DB-dependent rules from build-plan §4, given already-parsed
    rows. Split out from validate_file so the loader can run these once
    against the same parsed rows it's about to insert, rather than
    re-parsing the file a second time."""
    messages: list[Message] = []

    accounts = known_accounts(conn)
    aliases = alias_to_instrument(conn)
    alias_names = sorted(aliases)
    currencies_by_instrument = instrument_currencies(conn)
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
        if row.type != "BUY" or row.symbol is not None:
            continue
        try:
            weights = allocations.current_allocation(conn, row.account, row.entry_date)
        except allocations.NoAllocationError as exc:
            messages.append(Message("error", line, entry_id, str(exc)))
            continue
        for instrument_id, _weight in weights:
            if allocations.resolve_price_with_date(conn, instrument_id, row.entry_date) is None:
                messages.append(
                    Message(
                        "error", line, entry_id,
                        f"no price for {instrument_id} on or before "
                        f"{row.entry_date.isoformat()} — run `folios value`/"
                        f"`folios prices` first",
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
            currencies_by_instrument.get(instrument_id) if instrument_id else None
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
        if row.symbol is None or row.type not in QUANTITY_SIGN or row.quantity is None:
            continue
        instrument_id = aliases.get(row.symbol)
        if instrument_id is None:
            continue  # already reported above as an unresolved symbol
        key = (row.account, instrument_id)
        if key not in positions:
            positions[key] = _existing_position(
                conn, row.account, instrument_id, file_entry_ids
            )
        positions[key] += QUANTITY_SIGN[row.type] * row.quantity
        if row.type == "SELL" and positions[key] < 0:
            messages.append(
                Message(
                    "error", line, entry_id,
                    f"SELL drives the position for {row.symbol} in "
                    f"{row.account} negative ({positions[key]})",
                )
            )

    existing_hashes = existing_txn_hashes(conn, file_entry_ids)
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


def validate_file(conn: psycopg.Connection, path: Path) -> list[Message]:
    """Every rule in build-plan §4. A dry run: never writes to
    core.transactions, only reads reference data to check against."""
    parsed, messages = parse_rows(path)
    messages += validate_parsed(conn, parsed)
    return messages
