from typing import Any

import pytest

from folios.google import forms as google_forms
from folios.seed import seed
from tests.test_seed import EXAMPLE_CONFIG


@pytest.fixture()
def seeded_conn(migrated_conn):
    seed(migrated_conn, EXAMPLE_CONFIG)
    return migrated_conn


@pytest.fixture(autouse=True)
def isolated_form_state(tmp_path, monkeypatch):
    monkeypatch.setattr(google_forms, "FORM_STATE_PATH", tmp_path / "form_state.json")
    return tmp_path


class _Exec:
    def __init__(self, result: dict[str, Any]):
        self._result = result

    def execute(self) -> dict[str, Any]:
        return self._result


class FakeFormsService:
    """Stands in for googleapiclient's forms().* chain — applies
    createItem/updateItem requests to an in-memory item list exactly like
    the real API applies them (in request order, against `location.index`
    at the time each request is processed), so create_form's offset
    arithmetic is exercised for real, not just asserted about."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self._next_id = 1
        self.form_id = "fake-form-id"
        self.batch_calls: list[list[dict[str, Any]]] = []

    def forms(self) -> "FakeFormsService":
        return self

    def create(self, body: dict[str, Any]) -> _Exec:
        return _Exec({"formId": self.form_id, "responderUri": "https://forms.example/fake"})

    def get(self, formId: str) -> _Exec:  # noqa: N803 - matches googleapiclient's kwarg name
        return _Exec(
            {
                "formId": self.form_id,
                "items": [dict(item) for item in self.items],
                "responderUri": "https://forms.example/fake",
            }
        )

    def batchUpdate(self, formId: str, body: dict[str, Any]) -> _Exec:  # noqa: N802, N803
        requests = body["requests"]
        self.batch_calls.append(requests)
        replies: list[dict[str, Any]] = []
        for req in requests:
            if "createItem" in req:
                item_id = f"item-{self._next_id}"
                self._next_id += 1
                index = req["createItem"]["location"]["index"]
                item = dict(req["createItem"]["item"])
                item["itemId"] = item_id
                self.items.insert(index, item)
                replies.append({"createItem": {"itemId": item_id}})
            elif "updateItem" in req:
                item_id = req["updateItem"]["item"]["itemId"]
                for existing in self.items:
                    if existing["itemId"] == item_id:
                        existing["questionItem"] = req["updateItem"]["item"]["questionItem"]
                        break
                replies.append({"updateItem": {}})
        return _Exec({"replies": replies})


class FakeSheetsService:
    def __init__(self) -> None:
        self.created_body: dict[str, Any] | None = None

    def spreadsheets(self) -> "FakeSheetsService":
        return self

    def create(self, body: dict[str, Any]) -> _Exec:
        self.created_body = body
        return _Exec({"spreadsheetId": "fake-sheet-id"})


def _item_titles(service: FakeFormsService) -> list[str]:
    return [item["title"] for item in service.items]


def test_dimension_codes_currency_comes_from_config(seeded_conn, monkeypatch):
    monkeypatch.setattr(google_forms.fx, "currencies_from_config", lambda: {"USD", "EUR", "GBP"})
    codes = google_forms.dimension_codes(seeded_conn, "currency")
    assert codes == ["EUR", "GBP", "USD"]


def test_dimension_codes_reads_core_dimensions(seeded_conn):
    codes = google_forms.dimension_codes(seeded_conn, "asset_class")
    assert "EQUITY" in codes
    assert "CRYPTO" in codes


def test_symbol_choices_includes_aliases_and_not_listed_sentinel(seeded_conn):
    choices = google_forms.symbol_choices(seeded_conn)
    assert "DEMO" in choices
    assert "WORLD" in choices
    assert choices[-1] == google_forms.NOT_LISTED


def test_create_form_builds_expected_item_order_and_routing(seeded_conn):
    service = FakeFormsService()

    result = google_forms.create_form(seeded_conn, service)

    assert result["form_id"] == service.form_id
    assert result["responder_uri"] == "https://forms.example/fake"

    titles = _item_titles(service)
    assert titles[0:3] == ["Date", "Account", "Type"]

    section_titles = [title for _, title, _, _ in google_forms._all_sections()]
    for title in section_titles:
        assert title in titles

    # Each page break is immediately followed by its own section's fields,
    # in section order, with the routing block untouched at the front.
    trade_index = titles.index("Trade")
    assert titles[trade_index + 1 : trade_index + 9] == [
        "Symbol", "Quantity", "Price", "Gross", "Fee", "Tax", "Currency", "Note",
    ]

    valuation_index = titles.index("Valuation")
    assert titles[valuation_index + 1 : valuation_index + 4] == ["Symbol", "Price", "Currency"]

    # Type's options route to the right page break itemIds.
    type_item = service.items[titles.index("Type")]
    type_choice_question = type_item["questionItem"]["question"]["choiceQuestion"]
    type_options = {o["value"]: o["goToSectionId"] for o in type_choice_question["options"]}
    trade_page_break_id = service.items[trade_index]["itemId"]
    assert type_options["BUY"] == trade_page_break_id
    assert type_options["SELL"] == trade_page_break_id

    valuation_page_break_id = service.items[valuation_index]["itemId"]
    assert type_options[google_forms.VALUATION_TYPE] == valuation_page_break_id

    # A Symbol field's "not listed" option routes to New instrument.
    new_instrument_index = titles.index("New instrument")
    new_instrument_page_break_id = service.items[new_instrument_index]["itemId"]
    trade_symbol_item = service.items[trade_index + 1]
    not_listed_option = next(
        o
        for o in trade_symbol_item["questionItem"]["question"]["choiceQuestion"]["options"]
        if o["value"] == google_forms.NOT_LISTED
    )
    assert not_listed_option["goToSectionId"] == new_instrument_page_break_id


def test_form_init_saves_state(seeded_conn, monkeypatch):
    forms_service = FakeFormsService()
    sheets_service = FakeSheetsService()
    monkeypatch.setattr(google_forms, "get_credentials", lambda: "fake-creds")
    monkeypatch.setattr(google_forms, "build_forms_service", lambda creds=None: forms_service)
    monkeypatch.setattr(google_forms, "build_sheets_service", lambda creds=None: sheets_service)

    state = google_forms.form_init(seeded_conn)

    assert state["form_id"] == forms_service.form_id
    assert state["sheet_id"] == "fake-sheet-id"
    assert google_forms.load_form_state() == state
    assert sheets_service.created_body["sheets"][0]["properties"]["title"] == "Responses"


def test_form_sync_without_form_init_raises(seeded_conn):
    with pytest.raises(google_forms.FormNotInitializedError):
        google_forms.form_sync(seeded_conn)


def test_form_sync_updates_only_config_driven_dropdowns(seeded_conn, monkeypatch):
    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)
    google_forms._save_form_state({"form_id": service.form_id, "sheet_id": "fake-sheet-id"})
    monkeypatch.setattr(google_forms, "build_forms_service", lambda creds=None: service)

    with seeded_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.accounts (account_id, broker, institution, account_type, "
            "fiscal_envelope, custody_type, base_currency, opened_on) VALUES "
            "('NEW-ACCOUNT', 'B', 'I', 'securities', 'CTO', 'BROKER', 'EUR', '2026-01-01')"
        )
    seeded_conn.commit()

    result = google_forms.form_sync(seeded_conn)

    account_item = next(item for item in service.items if item["title"] == "Account")
    account_values = [
        o["value"] for o in account_item["questionItem"]["question"]["choiceQuestion"]["options"]
    ]
    assert "NEW-ACCOUNT" in account_values

    # Structure-only items (page breaks, Date, Type) are never touched.
    updated_titles = {
        req["updateItem"]["item"]["title"] for call in service.batch_calls[-1:] for req in call
    }
    assert "Date" not in updated_titles
    assert "Type" not in updated_titles
    assert result["updated_items"] == len(service.batch_calls[-1])


def test_form_sync_preserves_not_listed_routing(seeded_conn, monkeypatch):
    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)
    google_forms._save_form_state({"form_id": service.form_id, "sheet_id": "fake-sheet-id"})
    monkeypatch.setattr(google_forms, "build_forms_service", lambda creds=None: service)

    google_forms.form_sync(seeded_conn)

    trade_symbol_item = next(
        item
        for item in service.items
        if item["title"] == "Symbol"
        and any(
            o["value"] == google_forms.NOT_LISTED
            for o in item["questionItem"]["question"]["choiceQuestion"]["options"]
        )
    )
    not_listed_option = next(
        o
        for o in trade_symbol_item["questionItem"]["question"]["choiceQuestion"]["options"]
        if o["value"] == google_forms.NOT_LISTED
    )
    new_instrument_item = next(item for item in service.items if item["title"] == "New instrument")
    assert not_listed_option["goToSectionId"] == new_instrument_item["itemId"]
