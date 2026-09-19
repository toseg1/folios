import shutil
from datetime import date
from typing import Any

import pytest

from folios import new_instrument
from folios import seed as seed_module
from folios.google import forms as google_forms
from folios.google import sheets as google_sheets
from folios.seed import seed
from tests.test_seed import EXAMPLE_CONFIG


@pytest.fixture()
def seeded_conn(migrated_conn):
    seed(migrated_conn, EXAMPLE_CONFIG)
    return migrated_conn


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(google_sheets, "PULL_STATE_PATH", tmp_path / "pull_state.json")
    monkeypatch.setattr(google_sheets, "MANUAL_DIR", tmp_path / "manual")
    monkeypatch.setattr(google_forms, "FORM_STATE_PATH", tmp_path / "form_state.json")
    return tmp_path


FORM_ITEMS = [
    {"title": "Date", "questionItem": {"question": {"questionId": "q_date"}}},
    {"title": "Account", "questionItem": {"question": {"questionId": "q_account"}}},
    {"title": "Type", "questionItem": {"question": {"questionId": "q_type"}}},
    {"title": "Trade", "pageBreakItem": {}},
    {"title": "Symbol", "questionItem": {"question": {"questionId": "q_symbol"}}},
    {"title": "Quantity", "questionItem": {"question": {"questionId": "q_quantity"}}},
    {"title": "Price", "questionItem": {"question": {"questionId": "q_price"}}},
    {"title": "Gross", "questionItem": {"question": {"questionId": "q_gross"}}},
    {"title": "Fee", "questionItem": {"question": {"questionId": "q_fee"}}},
    {"title": "Tax", "questionItem": {"question": {"questionId": "q_tax"}}},
    {"title": "Currency", "questionItem": {"question": {"questionId": "q_currency"}}},
    {"title": "Note", "questionItem": {"question": {"questionId": "q_note"}}},
    {"title": "New instrument", "pageBreakItem": {}},
    {"title": "Name", "questionItem": {"question": {"questionId": "q_name"}}},
    {"title": "Yahoo ticker", "questionItem": {"question": {"questionId": "q_ticker"}}},
    {"title": "ISIN", "questionItem": {"question": {"questionId": "q_isin"}}},
    {"title": "Asset class", "questionItem": {"question": {"questionId": "q_asset_class"}}},
    # Deliberately a second "Currency" question, distinct from Trade's
    # own — mirrors the real form, where New instrument has its own
    # Currency field too. See _question_section_and_title/_response_fields.
    {"title": "Currency", "questionItem": {"question": {"questionId": "q_ni_currency"}}},
]

_TITLE_TO_QUESTION_ID = {
    item["title"]: item["questionItem"]["question"]["questionId"]
    for item in FORM_ITEMS
    if "questionItem" in item
}
# The comprehension above keeps the last "Currency" (New instrument's) —
# pin the bare "Currency" kwarg back to Trade's, since that's what every
# existing test means by it, and expose New instrument's under its own
# name for the one test that needs both at once.
_TITLE_TO_QUESTION_ID["Currency"] = "q_currency"
_TITLE_TO_QUESTION_ID["New instrument Currency"] = "q_ni_currency"


def _answer(value: str) -> dict[str, Any]:
    return {"textAnswers": {"answers": [{"value": value}]}}


def _response(response_id: str, **fields: str) -> dict[str, Any]:
    return {
        "responseId": response_id,
        "createTime": "2026-01-10T12:00:00Z",
        "lastSubmittedTime": "2026-01-10T12:00:00Z",
        "answers": {
            _TITLE_TO_QUESTION_ID[title]: _answer(value) for title, value in fields.items()
        },
    }


class _Exec:
    def __init__(self, result: dict[str, Any]):
        self._result = result

    def execute(self) -> dict[str, Any]:
        return self._result


class FakePullFormsService:
    def __init__(self, responses: list[dict[str, Any]], items: list[dict[str, Any]] | None = None):
        self._responses = responses
        self._items = items if items is not None else FORM_ITEMS

    def forms(self) -> "FakePullFormsService":
        return self

    def get(self, formId: str) -> _Exec:  # noqa: N803
        return _Exec({"formId": formId, "items": self._items})

    def responses(self) -> "FakePullFormsService":
        return self

    def list(self, formId: str, pageToken: str | None = None) -> _Exec:  # noqa: N803
        return _Exec({"responses": self._responses})


class FakeSheetsService:
    def __init__(self) -> None:
        self.appended_rows: list[list[str]] = []

    def spreadsheets(self) -> "FakeSheetsService":
        return self

    def values(self) -> "FakeSheetsService":
        return self

    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):  # noqa: N803
        self.appended_rows.extend(body["values"])
        return _Exec({})


class FakeTickerValidator:
    def __init__(self, valid: bool = True):
        self.valid = valid
        self.checked: list[str] = []

    def has_history(self, ticker: str) -> bool:
        self.checked.append(ticker)
        return self.valid


class FakeInfoProvider:
    """Stands in for YFinanceInfoProvider — never hits yfinance for real."""

    def fetch_info(self, ticker: str) -> dict:
        return {}


@pytest.fixture()
def writable_config_dir(tmp_path, monkeypatch):
    """A real, on-disk copy of config/example — new_instrument.py writes
    into seed.CONFIG_DIR for real, so tests must never point that at the
    actual project config/."""
    config_copy = tmp_path / "config"
    shutil.copytree(EXAMPLE_CONFIG, config_copy)
    monkeypatch.setattr(seed_module, "CONFIG_DIR", config_copy)
    return config_copy


def _save_form_state(tmp_path):
    google_forms._save_form_state({"form_id": "form-1", "sheet_id": "sheet-1"})


PULL_DATE = date(2026, 1, 10)


def _pull(conn, forms_service, sheets_service):
    return google_sheets.pull_responses(conn, forms_service, sheets_service, today=PULL_DATE)


def test_pull_without_form_init_raises(seeded_conn):
    forms_service = FakePullFormsService([])
    sheets_service = FakeSheetsService()
    with pytest.raises(google_forms.FormNotInitializedError):
        google_sheets.pull_responses(seeded_conn, forms_service, sheets_service)


def test_pull_writes_a_clean_buy_and_marks_it_ok(seeded_conn, isolated_state):
    _save_form_state(isolated_state)
    response = _response(
        "r1",
        Date="2026-01-10",
        Account="DEMO-BROKER-CTO",
        Type="BUY",
        Symbol="DEMO",
        Quantity="10",
        Price="100",
        Currency="EUR",
    )
    forms_service = FakePullFormsService([response])
    sheets_service = FakeSheetsService()

    result = _pull(seeded_conn, forms_service, sheets_service)

    assert result.responses_seen == 1
    assert result.transactions_written == 1
    assert result.errors == []

    txn_path = isolated_state / "manual" / "gform_2026-01-10.csv"
    assert txn_path.exists()
    content = txn_path.read_text()
    assert "DEMO-BROKER-CTO,BUY,DEMO,10,100" in content

    assert len(sheets_service.appended_rows) == 1
    row = sheets_service.appended_rows[0]
    assert row[0] == "r1"
    assert row[-2] == "ok"
    assert row[-1] == ""


def test_pull_routes_valuation_type_to_valuations_file(seeded_conn, isolated_state):
    _save_form_state(isolated_state)
    response = _response(
        "r-val",
        Date="2026-01-10",
        Account="DEMO-BROKER-CTO",
        Type=google_forms.VALUATION_TYPE,
        Symbol="FUNDBOND",
        Price="105.5",
        Currency="EUR",
    )
    forms_service = FakePullFormsService([response])
    sheets_service = FakeSheetsService()

    result = _pull(seeded_conn, forms_service, sheets_service)

    assert result.valuations_written == 1
    assert result.transactions_written == 0
    valuation_path = isolated_state / "manual" / "gform_valuations_2026-01-10.csv"
    assert valuation_path.exists()
    assert "FUNDBOND,105.5,EUR" in valuation_path.read_text()

    txn_path = isolated_state / "manual" / "gform_2026-01-10.csv"
    assert not txn_path.exists()


def test_pull_creates_new_instrument_from_not_listed_symbol(
    seeded_conn, isolated_state, writable_config_dir, monkeypatch
):
    _save_form_state(isolated_state)
    fake_validator = FakeTickerValidator(valid=True)
    monkeypatch.setattr(new_instrument, "YFinanceTickerValidator", lambda: fake_validator)
    monkeypatch.setattr(new_instrument, "YFinanceInfoProvider", FakeInfoProvider)

    response = _response(
        "r-new",
        **{
            "Date": "2026-01-10",
            "Account": "DEMO-BROKER-CTO",
            "Type": "BUY",
            "Symbol": google_forms.NOT_LISTED,
            "Quantity": "10",
            "Price": "100",
            "Currency": "EUR",
            "Name": "Demo Newco",
            "Yahoo ticker": "NEWCO.PA",
            "Asset class": "EQUITY",
            "New instrument Currency": "EUR",
        },
    )
    forms_service = FakePullFormsService([response])
    sheets_service = FakeSheetsService()

    result = _pull(seeded_conn, forms_service, sheets_service)

    # The instrument is created AND the trade the respondent was
    # mid-way through entering completes in the same pull, now that
    # Symbol resolves to the freshly created alias — the response
    # carries both pages' answers at once, so there's nothing to lose.
    assert result.new_instruments == ["NEWCO-PA"]
    assert result.transactions_written == 1
    assert result.errors == []
    assert fake_validator.checked == ["NEWCO.PA"]

    row = sheets_service.appended_rows[0]
    assert row[-2] == "ok"
    assert "NEWCO-PA" in row[-1]

    txn_path = isolated_state / "manual" / "gform_2026-01-10.csv"
    assert "DEMO-BROKER-CTO,BUY,Demo Newco - NEWCO-PA,10,100" in txn_path.read_text()

    instruments_csv = (writable_config_dir / "instruments.csv").read_text()
    assert "NEWCO-PA" in instruments_csv
    aliases_csv = (writable_config_dir / "aliases.csv").read_text()
    assert "NEWCO-PA" in aliases_csv

    # Reconciled into the database immediately, not just written to config.
    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT instrument_id FROM core.instrument_aliases "
            "WHERE alias = 'Demo Newco - NEWCO-PA'"
        )
        assert cur.fetchone() == ("NEWCO-PA",)


def test_pull_keeps_trade_and_new_instrument_currencies_separate(
    seeded_conn, isolated_state, writable_config_dir, monkeypatch
):
    # Trade and New instrument each have their own "Currency" question —
    # buying a USD-denominated instrument from a EUR account must not let
    # one page's answer clobber the other's.
    _save_form_state(isolated_state)
    fake_validator = FakeTickerValidator(valid=True)
    monkeypatch.setattr(new_instrument, "YFinanceTickerValidator", lambda: fake_validator)
    monkeypatch.setattr(new_instrument, "YFinanceInfoProvider", FakeInfoProvider)

    with seeded_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.fx_rates (rate_date, base_ccy, quote_ccy, rate) "
            "VALUES ('2026-01-10', 'EUR', 'USD', 1.08)"
        )
    seeded_conn.commit()

    response = _response(
        "r-new-mixed-currency",
        **{
            "Date": "2026-01-10",
            "Account": "DEMO-BROKER-CTO",
            "Type": "BUY",
            "Symbol": google_forms.NOT_LISTED,
            "Quantity": "10",
            "Price": "100",
            "Currency": "USD",
            "Name": "Demo Newco",
            "Yahoo ticker": "NEWCO.PA",
            "Asset class": "EQUITY",
            "New instrument Currency": "EUR",
        },
    )
    forms_service = FakePullFormsService([response])
    sheets_service = FakeSheetsService()

    result = _pull(seeded_conn, forms_service, sheets_service)

    assert result.new_instruments == ["NEWCO-PA"]
    assert result.transactions_written == 1
    assert result.errors == []

    # The instrument keeps its own currency (EUR)...
    instruments_csv = (writable_config_dir / "instruments.csv").read_text()
    newco_line = next(line for line in instruments_csv.splitlines() if "NEWCO-PA" in line)
    assert ",EUR," in newco_line

    # ...while the trade keeps its own (USD) — neither clobbered the other.
    txn_path = isolated_state / "manual" / "gform_2026-01-10.csv"
    content = txn_path.read_text()
    assert "DEMO-BROKER-CTO,BUY,Demo Newco - NEWCO-PA,10,100" in content
    assert ",USD," in content


def test_pull_routes_continuation_page_answers_into_new_instrument_fields(
    seeded_conn, isolated_state, writable_config_dir, monkeypatch
):
    # Regression test for the New-Instrument-family section-title fix: a
    # response can carry answers from the landing "New instrument" page
    # AND whichever continuation page (here, Bond) Asset class routed it
    # to — all of those must reach create_from_form_fields, not just the
    # landing page's own (a literal `section == "New instrument"` check
    # would silently drop everything from the continuation page).
    _save_form_state(isolated_state)
    fake_validator = FakeTickerValidator(valid=True)
    monkeypatch.setattr(new_instrument, "YFinanceTickerValidator", lambda: fake_validator)

    items = [
        *FORM_ITEMS,
        {"title": "New instrument — Bond details", "pageBreakItem": {}},
        {"title": "Coupon rate", "questionItem": {"question": {"questionId": "q_coupon_rate"}}},
        {"title": "Is callable", "questionItem": {"question": {"questionId": "q_is_callable"}}},
    ]
    response = {
        "responseId": "r-bond",
        "createTime": "2026-01-10T12:00:00Z",
        "lastSubmittedTime": "2026-01-10T12:00:00Z",
        "answers": {
            "q_date": _answer("2026-01-10"),
            "q_account": _answer("DEMO-BROKER-CTO"),
            "q_type": _answer("BUY"),
            "q_symbol": _answer(google_forms.NOT_LISTED),
            "q_quantity": _answer("10"),
            "q_price": _answer("100"),
            "q_currency": _answer("EUR"),
            "q_name": _answer("Demo Bond Co"),
            "q_ticker": _answer("BONDCO.PA"),
            "q_asset_class": _answer("BOND"),
            "q_ni_currency": _answer("EUR"),
            "q_coupon_rate": _answer("0.04"),
            "q_is_callable": _answer("false"),
        },
    }
    forms_service = FakePullFormsService([response], items=items)
    sheets_service = FakeSheetsService()

    result = _pull(seeded_conn, forms_service, sheets_service)

    assert result.new_instruments == ["BONDCO-PA"]
    assert result.errors == []

    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT coupon_rate, is_callable FROM core.instrument_bond "
            "WHERE instrument_id = 'BONDCO-PA'"
        )
        row = cur.fetchone()
    assert row is not None, "Bond continuation-page answers never reached create_from_form_fields"
    assert row[1] is False


def test_pull_marks_error_when_ticker_has_no_history(
    seeded_conn, isolated_state, writable_config_dir, monkeypatch
):
    _save_form_state(isolated_state)
    fake_validator = FakeTickerValidator(valid=False)
    monkeypatch.setattr(new_instrument, "YFinanceTickerValidator", lambda: fake_validator)

    response = _response(
        "r-bad-ticker",
        **{
            "Date": "2026-01-10",
            "Account": "DEMO-BROKER-CTO",
            "Type": "BUY",
            "Symbol": google_forms.NOT_LISTED,
            "Name": "Demo Newco",
            "Yahoo ticker": "NOTREAL",
            "Asset class": "EQUITY",
            "Currency": "EUR",
            "New instrument Currency": "EUR",
        },
    )
    forms_service = FakePullFormsService([response])
    sheets_service = FakeSheetsService()

    result = _pull(seeded_conn, forms_service, sheets_service)

    assert result.new_instruments == []
    assert len(result.errors) == 1
    assert "NOTREAL" in result.errors[0]
    row = sheets_service.appended_rows[0]
    assert row[-2] == "error"


def test_pull_marks_broken_submission_as_error_naming_the_field(seeded_conn, isolated_state):
    _save_form_state(isolated_state)
    response = _response(
        "r-bad",
        Date="2026-01-10",
        Account="NOT-A-REAL-ACCOUNT",
        Type="BUY",
        Symbol="DEMO",
        Quantity="10",
        Price="100",
        Currency="EUR",
    )
    forms_service = FakePullFormsService([response])
    sheets_service = FakeSheetsService()

    result = _pull(seeded_conn, forms_service, sheets_service)

    assert result.transactions_written == 0
    assert len(result.errors) == 1
    assert "NOT-A-REAL-ACCOUNT" in result.errors[0]

    row = sheets_service.appended_rows[0]
    assert row[-2] == "error"
    assert "account" in row[-1].lower() or "NOT-A-REAL-ACCOUNT" in row[-1]


def test_pull_fills_currency_from_instrument_for_transfer(seeded_conn, isolated_state):
    # Transfer has no Currency question at all — pull must fill it in
    # from core.instruments, same as the `folios add` wizard does.
    _save_form_state(isolated_state)
    response = _response(
        "r-transfer",
        Date="2026-01-10",
        Account="DEMO-BROKER-CTO",
        Type="TRANSFER_IN",
        Symbol="DEMO",
        Quantity="5",
    )
    forms_service = FakePullFormsService([response])
    sheets_service = FakeSheetsService()

    result = _pull(seeded_conn, forms_service, sheets_service)

    assert result.transactions_written == 1
    txn_path = isolated_state / "manual" / "gform_2026-01-10.csv"
    assert ",EUR," in txn_path.read_text()


def test_pull_is_idempotent_on_repeat_runs(seeded_conn, isolated_state):
    _save_form_state(isolated_state)
    response = _response(
        "r1",
        Date="2026-01-10",
        Account="DEMO-BROKER-CTO",
        Type="BUY",
        Symbol="DEMO",
        Quantity="10",
        Price="100",
        Currency="EUR",
    )
    forms_service = FakePullFormsService([response])
    sheets_service = FakeSheetsService()

    first = _pull(seeded_conn, forms_service, sheets_service)
    second = _pull(seeded_conn, forms_service, sheets_service)

    assert first.transactions_written == 1
    assert second.responses_seen == 0
    assert second.transactions_written == 0

    txn_path = isolated_state / "manual" / "gform_2026-01-10.csv"
    # Header + exactly one data row, not two.
    assert len(txn_path.read_text().strip().splitlines()) == 2
