from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from folios.models import (
    EntryRow,
    build_entry_id,
    compute_net_amount,
    compute_signed_quantity,
    compute_txn_hash,
    effective_gross,
)


def _row(**overrides):
    defaults = dict(
        entry_date="2026-01-10",
        account="DEMO-BROKER-CTO",
        type="BUY",
        symbol="DEMO",
        quantity="10",
        price="100",
        gross="1000",
        fee="1",
        tax="0",
        currency="EUR",
        note=None,
    )
    defaults.update(overrides)
    return EntryRow(**defaults)


def test_valid_row_parses():
    row = _row()
    assert row.entry_date == date(2026, 1, 10)
    assert row.quantity == Decimal("10")


def test_future_date_rejected():
    future = (date.today() + timedelta(days=1)).isoformat()
    with pytest.raises(ValidationError, match="future"):
        _row(entry_date=future, type="DEPOSIT", symbol=None, quantity=None, price=None)


def test_unknown_type_rejected():
    with pytest.raises(ValidationError, match="not a known transaction type"):
        _row(type="NOT_A_TYPE")


def test_bad_currency_shape_rejected():
    with pytest.raises(ValidationError, match="ISO 4217"):
        _row(currency="EU")


def test_negative_quantity_rejected():
    with pytest.raises(ValidationError, match="positive numbers only"):
        _row(quantity="-10")


def test_gross_reconciles_within_one_cent():
    row = _row(quantity="10", price="100.001", gross="1000.01")
    assert row.gross == Decimal("1000.01")


def test_gross_mismatch_rejected():
    with pytest.raises(ValidationError, match="does not reconcile"):
        _row(quantity="10", price="100", gross="500")


def test_standalone_fee_row_rejects_populated_fee_column():
    with pytest.raises(ValidationError, match="carries its amount in gross"):
        _row(
            type="FEE", symbol=None, quantity=None, price=None, gross="5",
            fee="1", tax="0",
        )


def test_standalone_fee_row_with_blank_fee_tax_is_fine():
    row = _row(
        type="FEE", symbol=None, quantity=None, price=None, gross="5",
        fee="0", tax="0",
    )
    assert row.type == "FEE"


def test_blank_symbol_and_note_become_none():
    row = _row(symbol="", note="")
    assert row.symbol is None
    assert row.note is None


def test_build_entry_id():
    assert build_entry_id("data/manual/transactions.csv", 7) == (
        "csv:data/manual/transactions.csv:7"
    )


def test_compute_txn_hash_is_stable_and_content_sensitive():
    row = {"date": "2026-01-10", "account": "A", "type": "BUY", "symbol": "X",
           "quantity": "1", "price": "1", "gross": "1", "fee": "0", "tax": "0",
           "currency": "EUR", "note": ""}
    h1 = compute_txn_hash(row)
    h2 = compute_txn_hash(dict(row))
    assert h1 == h2

    edited = dict(row, note="changed")
    assert compute_txn_hash(edited) != h1


def test_effective_gross_uses_gross_when_present():
    row = _row(gross="1000")
    assert effective_gross(row) == Decimal("1000")


def test_effective_gross_computed_when_blank():
    row = _row(quantity="10", price="100", gross=None)
    assert effective_gross(row) == Decimal("1000")


def test_effective_gross_none_when_nothing_to_compute_from():
    row = _row(
        type="DEPOSIT", symbol=None, quantity=None, price=None, gross=None,
    )
    assert effective_gross(row) is None


def test_compute_signed_quantity_buy_is_positive():
    row = _row(type="BUY", quantity="10")
    assert compute_signed_quantity(row) == Decimal("10")


def test_compute_signed_quantity_sell_is_negative():
    row = _row(type="SELL", quantity="10")
    assert compute_signed_quantity(row) == Decimal("-10")


def test_compute_signed_quantity_none_when_no_quantity_concept():
    row = _row(
        type="DIVIDEND", symbol="DEMO", quantity=None, price=None, gross="10",
    )
    assert compute_signed_quantity(row) is None


def test_compute_net_amount_buy_is_negative_cost_plus_fee():
    row = _row(type="BUY", quantity="10", price="100", gross="1000", fee="1")
    assert compute_net_amount(row) == Decimal("-1001")


def test_compute_net_amount_sell_is_proceeds_minus_fee():
    row = _row(type="SELL", quantity="10", price="100", gross="1000", fee="1")
    assert compute_net_amount(row) == Decimal("999")


def test_compute_net_amount_dividend_is_gross_minus_tax():
    row = _row(
        type="DIVIDEND", symbol="DEMO", quantity=None, price=None,
        gross="9.60", fee="0", tax="1.44",
    )
    assert compute_net_amount(row) == Decimal("8.16")


def test_compute_net_amount_staking_is_zero():
    row = _row(type="STAKING", quantity="5", price=None, gross="500", fee="0", tax="0")
    assert compute_net_amount(row) == Decimal("0")


def test_compute_net_amount_fee_row_is_negative_gross():
    row = _row(
        type="FEE", symbol=None, quantity=None, price=None, gross="5",
        fee="0", tax="0",
    )
    assert compute_net_amount(row) == Decimal("-5")


def test_compute_net_amount_deposit_is_positive_gross():
    row = _row(
        type="DEPOSIT", symbol=None, quantity=None, price=None, gross="2000",
        fee="0", tax="0",
    )
    assert compute_net_amount(row) == Decimal("2000")


def test_compute_net_amount_uses_effective_gross_when_blank():
    row = _row(type="BUY", quantity="10", price="100", gross=None, fee="0", tax="0")
    assert compute_net_amount(row) == Decimal("-1000")
