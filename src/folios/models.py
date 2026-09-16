from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, field_validator, model_validator

# Kept in sync by hand with the txn_type CHECK in
# migrations/001_schemas_and_core.sql — a new type needs a migration
# anyway (build-plan §ADR: "dimensions are data, transaction types are
# code"), so duplicating the list here is deliberate, not an oversight.
TXN_TYPES = frozenset(
    {
        "BUY", "SELL", "DIVIDEND", "INTEREST", "STAKING", "FEE", "TAX",
        "DEPOSIT", "WITHDRAWAL", "TRANSFER_IN", "TRANSFER_OUT", "SPLIT",
        "OPENING_BALANCE",
    }
)

# Types with no cash effect of their own (net_amount = 0), or none of
# quantity/price/gross/fee/tax reconciliation applies the same way.
_COST_ONLY_TYPES = {"FEE", "TAX"}

ENTRY_CSV_COLUMNS = (
    "date", "account", "type", "symbol", "quantity", "price", "gross",
    "fee", "tax", "currency", "note",
)


def _parse_decimal(value: Any, field_name: str) -> Decimal | None:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field_name}={value!r} is not a valid number") from exc
    if parsed < 0:
        raise ValueError(
            f"{field_name}={value!r} is negative — the entry CSV takes positive "
            f"numbers only, the loader applies the sign from type"
        )
    return parsed


class EntryRow(BaseModel):
    """One row of data/manual/transactions.csv (or a data/samples/ fixture).

    Row-intrinsic validation only — type shape, arithmetic consistency
    between this row's own fields. Anything needing the database (account
    exists, symbol resolves, FX rate exists, running position) lives in
    validate.py, which runs after every row here has already parsed clean.
    """

    entry_date: date
    account: str
    type: str
    symbol: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    gross: Decimal | None = None
    fee: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")
    currency: str
    note: str | None = None

    @field_validator("entry_date", mode="before")
    @classmethod
    def _parse_date(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return date.fromisoformat(value.strip())
            except ValueError as exc:
                raise ValueError(f"date={value!r} does not parse as YYYY-MM-DD") from exc
        return value

    @field_validator("entry_date")
    @classmethod
    def _date_not_in_future(cls, value: date) -> date:
        if value > date.today():
            raise ValueError(f"date={value.isoformat()} is in the future")
        return value

    @field_validator("account", "currency", mode="before")
    @classmethod
    def _strip_required(cls, value: Any) -> Any:
        if value is None or str(value).strip() == "":
            raise ValueError("must not be blank")
        return str(value).strip()

    @field_validator("symbol", "note", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if value is None or str(value).strip() == "":
            return None
        return str(value).strip()

    @field_validator("type", mode="before")
    @classmethod
    def _type_in_enum(cls, value: Any) -> Any:
        value = str(value).strip().upper() if value is not None else value
        if value not in TXN_TYPES:
            raise ValueError(
                f"type={value!r} is not a known transaction type "
                f"(one of {', '.join(sorted(TXN_TYPES))})"
            )
        return value

    @field_validator("currency", mode="after")
    @classmethod
    def _currency_shape(cls, value: str) -> str:
        value = value.upper()
        if len(value) != 3 or not value.isalpha():
            raise ValueError(f"currency={value!r} is not a 3-letter ISO 4217 code")
        return value

    @field_validator("quantity", "price", "gross", mode="before")
    @classmethod
    def _optional_positive_decimal(cls, value: Any, info: Any) -> Any:
        return _parse_decimal(value, info.field_name)

    @field_validator("fee", "tax", mode="before")
    @classmethod
    def _required_positive_decimal(cls, value: Any, info: Any) -> Any:
        parsed = _parse_decimal(value, info.field_name)
        return parsed if parsed is not None else Decimal("0")

    @model_validator(mode="after")
    def _gross_reconciles_with_quantity_and_price(self) -> EntryRow:
        if self.quantity is not None and self.price is not None and self.gross is not None:
            expected = self.quantity * self.price
            if abs(expected - self.gross) > Decimal("0.01"):
                raise ValueError(
                    f"quantity ({self.quantity}) x price ({self.price}) = "
                    f"{expected}, which does not reconcile with gross "
                    f"({self.gross}) within 1 cent"
                )
        return self

    @model_validator(mode="after")
    def _standalone_cost_rows_carry_no_fee_or_tax(self) -> EntryRow:
        # Mirrors the DB's own standalone_cost_rows_have_no_columns CHECK:
        # a FEE/TAX row's amount lives in `gross` alone. fee/tax populated
        # here would silently double-count once loaded.
        if self.type in _COST_ONLY_TYPES and (self.fee != 0 or self.tax != 0):
            raise ValueError(
                f"type={self.type} carries its amount in gross, not in "
                f"fee/tax — fee={self.fee}, tax={self.tax} should both be 0"
            )
        return self


def build_entry_id(relative_path: str, line_number: int) -> str:
    """Stable key: never a content hash, so correcting a typo doesn't
    orphan the old row. See build-plan §4."""
    return f"csv:{relative_path}:{line_number}"


def compute_txn_hash(raw_row: dict[str, Any]) -> str:
    """Content hash of the row's own fields, for change detection only —
    never used as the primary key. Order-independent: keyed by column
    name, not position."""
    canonical = "|".join(
        f"{col}={(raw_row.get(col) or '').strip()}" for col in ENTRY_CSV_COLUMNS
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
