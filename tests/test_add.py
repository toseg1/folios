from folios.add import FIELDS_FOR_TYPE, append_entry, suggest_symbols
from folios.models import TXN_TYPES


def test_every_txn_type_has_a_field_profile():
    assert set(FIELDS_FOR_TYPE) == TXN_TYPES


def test_buy_symbol_is_optional_quantity_price_gross_fee_tax_required():
    # Optional, not absent: leaving it blank makes this a general
    # contribution (config/account_allocations.csv), it doesn't mean BUY
    # has no symbol concept the way DEPOSIT does.
    fields = FIELDS_FOR_TYPE["BUY"]
    assert fields.symbol == "optional"
    assert fields.quantity and fields.price and fields.gross
    assert fields.fee and fields.tax


def test_deposit_has_no_symbol_quantity_or_price():
    fields = FIELDS_FOR_TYPE["DEPOSIT"]
    assert fields.symbol is None
    assert not fields.quantity
    assert not fields.price
    assert fields.gross


def test_interest_symbol_is_optional():
    assert FIELDS_FOR_TYPE["INTEREST"].symbol == "optional"


def test_suggest_symbols_finds_near_matches():
    assert "DEMO" in suggest_symbols("DEM", ["DEMO", "BTC", "OTHER"])


def test_suggest_symbols_empty_when_nothing_close():
    assert suggest_symbols("ZZZZZ", ["DEMO", "BTC"]) == []


def test_append_entry_creates_file_with_header(tmp_path):
    path = tmp_path / "transactions.csv"
    line_number, relative = append_entry(
        path,
        {"date": "2026-01-10", "account": "A", "type": "DEPOSIT", "symbol": "",
         "quantity": "", "price": "", "gross": "100", "fee": "0", "tax": "0",
         "currency": "EUR", "note": ""},
    )
    assert line_number == 2  # header is line 1
    assert path.read_text().splitlines()[0].startswith("date,account,type")
    assert "DEPOSIT" in path.read_text()


def test_append_entry_appends_after_existing_rows(tmp_path):
    path = tmp_path / "transactions.csv"
    append_entry(
        path,
        {"date": "2026-01-10", "account": "A", "type": "DEPOSIT", "symbol": "",
         "quantity": "", "price": "", "gross": "100", "fee": "0", "tax": "0",
         "currency": "EUR", "note": "first"},
    )
    line_number, _ = append_entry(
        path,
        {"date": "2026-01-11", "account": "A", "type": "DEPOSIT", "symbol": "",
         "quantity": "", "price": "", "gross": "50", "fee": "0", "tax": "0",
         "currency": "EUR", "note": "second"},
    )
    assert line_number == 3
    lines = path.read_text().splitlines()
    assert len(lines) == 3
    assert "second" in lines[2]
