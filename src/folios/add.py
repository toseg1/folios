from __future__ import annotations

import csv
import difflib
from pathlib import Path
from typing import NamedTuple

from folios.models import ENTRY_CSV_COLUMNS, TXN_TYPES
from folios.validate import relative_path


class TypeFields(NamedTuple):
    # symbol: None (not asked) | "required" | "optional"
    symbol: str | None
    quantity: bool
    price: bool
    gross: bool
    fee: bool
    tax: bool


# Which fields the interactive wizard prompts for, per txn_type — mirrors
# the Form's section-by-type table in the build plan (step 14), since the
# same "only show what this type needs" logic applies to the terminal.
FIELDS_FOR_TYPE: dict[str, TypeFields] = {
    "BUY": TypeFields("required", True, True, True, True, True),
    "SELL": TypeFields("required", True, True, True, True, True),
    "DIVIDEND": TypeFields("required", False, False, True, True, True),
    "INTEREST": TypeFields("optional", False, False, True, True, True),
    "STAKING": TypeFields("required", True, False, True, False, False),
    "FEE": TypeFields(None, False, False, True, False, False),
    "TAX": TypeFields(None, False, False, True, False, False),
    "DEPOSIT": TypeFields(None, False, False, True, False, False),
    "WITHDRAWAL": TypeFields(None, False, False, True, False, False),
    "TRANSFER_IN": TypeFields("required", True, False, False, False, False),
    "TRANSFER_OUT": TypeFields("required", True, False, False, False, False),
    "SPLIT": TypeFields("required", True, False, False, False, False),
    "OPENING_BALANCE": TypeFields("required", True, False, True, False, False),
}
assert set(FIELDS_FOR_TYPE) == TXN_TYPES, "every txn_type needs a prompt profile"


def suggest_symbols(symbol: str, known_aliases: list[str]) -> list[str]:
    return difflib.get_close_matches(symbol, known_aliases, n=3)


def append_entry(path: Path, raw_row: dict[str, str]) -> tuple[int, str]:
    """Append one row to a transactions CSV, creating it with a header
    first if it doesn't exist yet. Returns (new line number, path
    relative to the repo root) — entry_id is csv:<relative path>:<line>,
    same as every other entry in the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists()

    if file_exists:
        with path.open(newline="", encoding="utf-8") as f:
            line_count = sum(1 for _ in f)
    else:
        line_count = 0

    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ENTRY_CSV_COLUMNS)
        if not file_exists:
            writer.writeheader()
            line_count = 1
        writer.writerow(raw_row)

    return line_count + 1, relative_path(path)
