import shutil
from typing import Any

import pytest

from folios import seed as seed_module
from folios.google import forms as google_forms
from folios.seed import seed
from tests.test_seed import EXAMPLE_CONFIG


@pytest.fixture()
def seeded_conn(migrated_conn, monkeypatch):
    # dimension_codes()'s currency branch calls fx.currencies_from_config(),
    # which defaults to seed.CONFIG_DIR rather than taking the connection's
    # own config source — without this, it silently reads whatever real,
    # gitignored config/ happens to be on the machine running the tests.
    # On a dev machine with real config/accounts.yml + instruments.csv that
    # masks the bug; on a clean CI checkout (no real config/ at all) it
    # returns an empty currency set, which starves every Currency dropdown
    # this file builds. Point it at the same EXAMPLE_CONFIG actually seeded
    # into the DB below, so tests don't depend on what's on disk outside
    # the repo.
    monkeypatch.setattr(seed_module, "CONFIG_DIR", EXAMPLE_CONFIG)
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


def _item_in_section(
    service: FakeFormsService, section_title: str, field_label: str
) -> dict[str, Any]:
    current_section = None
    for item in service.items:
        if "pageBreakItem" in item:
            current_section = item.get("title")
            continue
        if current_section == section_title and item.get("title") == field_label:
            return item
    raise LookupError(f"no {field_label!r} item found in section {section_title!r}")


def _options(item: dict[str, Any]) -> list[dict[str, Any]]:
    return item["questionItem"]["question"]["choiceQuestion"]["options"]


def _assert_no_mixed_navigation(service: FakeFormsService) -> None:
    """Confirmed live: batchUpdate's updateItem 400s with "Invalid
    Options, Either all or no options should be go to enabled" on a
    ChoiceQuestion where some options carry goToAction/goToSectionId and
    others don't (createItem tolerates it, updateItem doesn't — this is
    exactly how the Valuation Symbol bug surfaced). FakeFormsService
    doesn't enforce this, so assert it explicitly."""
    for item in service.items:
        choice = item.get("questionItem", {}).get("question", {}).get("choiceQuestion")
        if not choice:
            continue
        has_nav = {"goToAction" in o or "goToSectionId" in o for o in choice["options"]}
        assert len(has_nav) <= 1, f"{item.get('title')!r} mixes navigating and plain options"


def test_dimension_codes_currency_comes_from_config(seeded_conn, monkeypatch):
    monkeypatch.setattr(google_forms.fx, "currencies_from_config", lambda: {"USD", "EUR", "GBP"})
    codes = google_forms.dimension_codes(seeded_conn, "currency")
    assert codes == ["EUR", "GBP", "USD"]


def test_dimension_codes_reads_from_config_even_if_db_disagrees(seeded_conn):
    # dimension_codes() must source choices from config/dimensions.csv,
    # not core.dimensions — seed() only ever upserts, so a code removed
    # from config/ would otherwise linger in the DB (and the dropdown)
    # forever. Proven here by deleting the DB's copy and confirming the
    # code still comes through from config/.
    with seeded_conn.cursor() as cur:
        cur.execute("DELETE FROM core.dimensions WHERE dimension = 'asset_class'")
    seeded_conn.commit()

    codes = google_forms.dimension_codes(seeded_conn, "asset_class")
    assert "EQUITY" in codes
    assert "CRYPTO" in codes


def test_account_choices_excludes_inactive_accounts(seeded_conn):
    # KRAKEN-EXCHANGE is is_active: false in config/example/accounts.yml
    # — a closed account must not be offered for new entries.
    choices = google_forms.account_choices()
    assert "KRAKEN-EXCHANGE" not in choices
    assert "LEDGER-SELFCUSTODY" in choices


def test_symbol_choices_includes_aliases_and_not_listed_sentinel(seeded_conn):
    choices = google_forms.symbol_choices(seeded_conn)
    assert "DEMO" in choices
    assert "WORLD" in choices
    assert choices[-1] == google_forms.NOT_LISTED


def test_symbol_choices_excludes_aliases_of_inactive_instruments(
    seeded_conn, monkeypatch, tmp_path
):
    config_copy = tmp_path / "config"
    shutil.copytree(EXAMPLE_CONFIG, config_copy)
    instruments_path = config_copy / "instruments.csv"
    instruments_path.write_text(
        instruments_path.read_text().replace(
            "WISDOMTREE-GOLD,,,,WisdomTree Physical Gold,FUND,ETN,EUR,,,,WisdomTree,GB,"
            "MARKET_EXPOSED,false,manual,true",
            "WISDOMTREE-GOLD,,,,WisdomTree Physical Gold,FUND,ETN,EUR,,,,WisdomTree,GB,"
            "MARKET_EXPOSED,false,manual,false",
        )
    )
    monkeypatch.setattr(seed_module, "CONFIG_DIR", config_copy)

    choices = google_forms.symbol_choices(seeded_conn)
    assert "GOLD" not in choices
    assert "DEMO" in choices


def test_symbol_choices_excluding_not_listed_falls_back_when_no_aliases_exist(
    seeded_conn, monkeypatch, tmp_path
):
    # Confirmed live: an empty ChoiceQuestion.options 400s on batchUpdate.
    # A fresh instance with no instruments configured yet — the user's
    # actual state when this regressed — must not produce zero options
    # for Income's Symbol dropdown (allow_new_instrument=False).
    config_copy = tmp_path / "config"
    shutil.copytree(EXAMPLE_CONFIG, config_copy)
    (config_copy / "aliases.csv").write_text("alias,source,instrument_id\n")
    monkeypatch.setattr(seed_module, "CONFIG_DIR", config_copy)

    choices = google_forms.symbol_choices(seeded_conn, include_not_listed=False)
    assert choices == [google_forms.NO_ALIASES_PLACEHOLDER]


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

    contribution_index = titles.index("Contribution")
    assert titles[contribution_index + 1 : contribution_index + 4] == [
        "Amount", "Currency", "Note",
    ]
    contribution_page_break_id = service.items[contribution_index]["itemId"]
    assert type_options[google_forms.CONTRIBUTION_TYPE] == contribution_page_break_id

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


def test_form_sync_updates_only_config_driven_dropdowns(seeded_conn, monkeypatch, tmp_path):
    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)
    google_forms._save_form_state({"form_id": service.form_id, "sheet_id": "fake-sheet-id"})
    monkeypatch.setattr(google_forms, "build_forms_service", lambda creds=None: service)

    config_copy = tmp_path / "config"
    shutil.copytree(EXAMPLE_CONFIG, config_copy)
    accounts_path = config_copy / "accounts.yml"
    accounts_path.write_text(
        accounts_path.read_text() + "\n"
        "- account_id: NEW-ACCOUNT\n"
        "  broker: B\n"
        "  ultimate_parent: I\n"
        "  account_type: securities\n"
        "  fiscal_envelope: CTO\n"
        "  custody_type: BROKER\n"
        "  base_currency: EUR\n"
        "  opened_on: 2026-01-01\n"
    )
    monkeypatch.setattr(seed_module, "CONFIG_DIR", config_copy)

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


def test_form_sync_refreshes_dimension_backed_dropdowns_beyond_account_symbol_currency(
    seeded_conn, monkeypatch, tmp_path
):
    # Regression test: form_sync used to special-case only titles
    # "Account", "Symbol" and "Currency" — a new config/dimensions.csv
    # code for asset_class/instrument_type/protection_type never reached
    # the live form until form-init rebuilt it from scratch.
    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)
    google_forms._save_form_state({"form_id": service.form_id, "sheet_id": "fake-sheet-id"})
    monkeypatch.setattr(google_forms, "build_forms_service", lambda creds=None: service)

    config_copy = tmp_path / "config"
    shutil.copytree(EXAMPLE_CONFIG, config_copy)
    dimensions_path = config_copy / "dimensions.csv"
    dimensions_path.write_text(
        dimensions_path.read_text()
        + "asset_class,COMMODITY,Commodity,60\n"
        "protection_type,CAPITAL_GUARANTEED,Capital guaranteed,20\n"
    )
    monkeypatch.setattr(seed_module, "CONFIG_DIR", config_copy)

    google_forms.form_sync(seeded_conn)

    def values(field_label: str) -> list[str]:
        item = _item_in_section(service, "New instrument", field_label)
        return [o["value"] for o in _options(item)]

    assert "COMMODITY" in values("Asset class")
    assert "CAPITAL_GUARANTEED" in values("Protection type")
    # Instrument type moved to the Fund/Crypto subtype pages and is
    # filtered by INSTRUMENT_TYPES_BY_ASSET_CLASS there — see
    # test_form_sync_drops_dimension_codes_removed_from_config for its
    # own config-driven-refresh coverage.


def test_form_sync_refreshes_peg_currency_despite_its_title_not_being_currency(
    seeded_conn, monkeypatch
):
    # Peg currency is dimension="currency" too, but the old title=="Currency"
    # special case never matched it.
    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)
    google_forms._save_form_state({"form_id": service.form_id, "sheet_id": "fake-sheet-id"})
    monkeypatch.setattr(google_forms, "build_forms_service", lambda creds=None: service)

    google_forms.form_sync(seeded_conn)

    updated_titles = {
        req["updateItem"]["item"]["title"] for call in service.batch_calls[-1:] for req in call
    }
    assert "Peg currency" in updated_titles


def test_form_sync_preserves_asset_class_routing_after_refresh(seeded_conn, monkeypatch):
    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)
    google_forms._save_form_state({"form_id": service.form_id, "sheet_id": "fake-sheet-id"})
    monkeypatch.setattr(google_forms, "build_forms_service", lambda creds=None: service)

    def page_break_id(title: str) -> str:
        return next(item["itemId"] for item in service.items if item.get("title") == title)

    fund_page_id = page_break_id("New instrument — Fund details")
    bond_page_id = page_break_id("New instrument — Bond details")
    crypto_page_id = page_break_id("New instrument — Crypto details")

    google_forms.form_sync(seeded_conn)

    asset_class_options = {
        o["value"]: o
        for o in _options(_item_in_section(service, "New instrument", "Asset class"))
    }
    assert asset_class_options["FUND"]["goToSectionId"] == fund_page_id
    assert asset_class_options["BOND"]["goToSectionId"] == bond_page_id
    assert asset_class_options["CRYPTO"]["goToSectionId"] == crypto_page_id
    assert asset_class_options["EQUITY"]["goToAction"] == "SUBMIT_FORM"
    assert "goToSectionId" not in asset_class_options["EQUITY"]


def test_form_sync_reconciles_config_edits_without_a_separate_init(
    seeded_conn, monkeypatch, tmp_path
):
    # Regression test: form-sync's own docstring says "run after editing
    # config/" — it must pick up a hand-edited config/aliases.csv itself,
    # not only aliases already reconciled into the DB by a prior `folios
    # init`.
    config_copy = tmp_path / "config"
    shutil.copytree(EXAMPLE_CONFIG, config_copy)
    aliases_path = config_copy / "aliases.csv"
    aliases_path.write_text(aliases_path.read_text() + "DEMO2,manual,DEMO-SHARE\n")
    monkeypatch.setattr(seed_module, "CONFIG_DIR", config_copy)

    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)
    google_forms._save_form_state({"form_id": service.form_id, "sheet_id": "fake-sheet-id"})
    monkeypatch.setattr(google_forms, "build_forms_service", lambda creds=None: service)

    google_forms.form_sync(seeded_conn)

    symbol_item = _item_in_section(service, "Trade", "Symbol")
    assert "DEMO2" in [o["value"] for o in _options(symbol_item)]


def test_form_sync_drops_dimension_codes_removed_from_config(seeded_conn, monkeypatch, tmp_path):
    # The actual bug report: seed() only upserts, it never deletes, so an
    # instrument_type removed from config/dimensions.csv stayed in
    # core.dimensions (and kept showing up in the dropdown) forever. ETC
    # is already seeded into the DB by the seeded_conn fixture below,
    # before this test's config copy removes it.
    config_copy = tmp_path / "config"
    shutil.copytree(EXAMPLE_CONFIG, config_copy)
    dimensions_path = config_copy / "dimensions.csv"
    dimensions_path.write_text(
        "".join(
            line
            for line in dimensions_path.read_text().splitlines(keepends=True)
            if not line.startswith("instrument_type,ETC,")
        )
    )
    monkeypatch.setattr(seed_module, "CONFIG_DIR", config_copy)

    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)
    google_forms._save_form_state({"form_id": service.form_id, "sheet_id": "fake-sheet-id"})
    monkeypatch.setattr(google_forms, "build_forms_service", lambda creds=None: service)

    google_forms.form_sync(seeded_conn)

    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM core.dimensions WHERE dimension='instrument_type' AND code='ETC'"
        )
        assert cur.fetchone() is not None, "sanity: seed() must not have deleted the stale row"

    instrument_type_item = _item_in_section(
        service, "New instrument — Fund details", "Instrument type"
    )
    instrument_type_values = [o["value"] for o in _options(instrument_type_item)]
    assert "ETC" not in instrument_type_values
    assert "ETF" in instrument_type_values


def test_form_sync_drops_accounts_removed_from_config(seeded_conn, monkeypatch, tmp_path):
    # Same bug, for the Account dropdown: CAISSE-EPARGNE-LIVRETA is
    # already seeded into core.accounts by the seeded_conn fixture below,
    # before this test's config copy removes it.
    config_copy = tmp_path / "config"
    shutil.copytree(EXAMPLE_CONFIG, config_copy)
    accounts_path = config_copy / "accounts.yml"
    accounts_path.write_text(
        "\n\n".join(
            block
            for block in accounts_path.read_text().split("\n\n")
            if "account_id: CAISSE-EPARGNE-LIVRETA" not in block
        )
    )
    monkeypatch.setattr(seed_module, "CONFIG_DIR", config_copy)

    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)
    google_forms._save_form_state({"form_id": service.form_id, "sheet_id": "fake-sheet-id"})
    monkeypatch.setattr(google_forms, "build_forms_service", lambda creds=None: service)

    google_forms.form_sync(seeded_conn)

    with seeded_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM core.accounts WHERE account_id='CAISSE-EPARGNE-LIVRETA'")
        assert cur.fetchone() is not None, "sanity: seed() must not have deleted the stale row"

    account_item = next(item for item in service.items if item["title"] == "Account")
    account_values = [o["value"] for o in _options(account_item)]
    assert "CAISSE-EPARGNE-LIVRETA" not in account_values
    assert "LEDGER-SELFCUSTODY" in account_values


def test_form_sync_drops_aliases_removed_from_config(seeded_conn, monkeypatch, tmp_path):
    # Same bug, for the Symbol dropdown: the GOLD alias is already seeded
    # into core.instrument_aliases by the seeded_conn fixture below,
    # before this test's config copy removes it.
    config_copy = tmp_path / "config"
    shutil.copytree(EXAMPLE_CONFIG, config_copy)
    aliases_path = config_copy / "aliases.csv"
    aliases_path.write_text(
        "".join(
            line
            for line in aliases_path.read_text().splitlines(keepends=True)
            if not line.startswith("GOLD,")
        )
    )
    monkeypatch.setattr(seed_module, "CONFIG_DIR", config_copy)

    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)
    google_forms._save_form_state({"form_id": service.form_id, "sheet_id": "fake-sheet-id"})
    monkeypatch.setattr(google_forms, "build_forms_service", lambda creds=None: service)

    google_forms.form_sync(seeded_conn)

    with seeded_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM core.instrument_aliases WHERE alias='GOLD'")
        assert cur.fetchone() is not None, "sanity: seed() must not have deleted the stale row"

    symbol_item = _item_in_section(service, "Trade", "Symbol")
    symbol_values = [o["value"] for o in _options(symbol_item)]
    assert "GOLD" not in symbol_values
    assert "DEMO" in symbol_values


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


def test_contribution_section_has_no_symbol_quantity_or_price(seeded_conn):
    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)

    labels_in_section: list[str] = []
    current_section = None
    for item in service.items:
        if "pageBreakItem" in item:
            current_section = item.get("title")
            continue
        if current_section == "Contribution":
            labels_in_section.append(item["title"])
    assert labels_in_section == ["Amount", "Currency", "Note"]


def test_form_sync_refreshes_contribution_currency_from_a_new_dimension_code(
    seeded_conn, monkeypatch, tmp_path
):
    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)
    google_forms._save_form_state({"form_id": service.form_id, "sheet_id": "fake-sheet-id"})
    monkeypatch.setattr(google_forms, "build_forms_service", lambda creds=None: service)

    config_copy = tmp_path / "config"
    shutil.copytree(EXAMPLE_CONFIG, config_copy)
    accounts_path = config_copy / "accounts.yml"
    # Introduces a currency not already used anywhere in EXAMPLE_CONFIG,
    # so it can only show up via fx.currencies_from_config() picking up
    # this new account's base_currency — same mechanism dimension_codes()
    # uses for every "currency" dropdown, Contribution's included.
    accounts_path.write_text(
        accounts_path.read_text() + "\n"
        "- account_id: NEW-CHF-ACCOUNT\n"
        "  broker: B\n"
        "  account_type: cash\n"
        "  fiscal_envelope: CURRENT\n"
        "  base_currency: CHF\n"
        "  opened_on: 2026-01-01\n"
    )
    monkeypatch.setattr(seed_module, "CONFIG_DIR", config_copy)

    google_forms.form_sync(seeded_conn)

    currency_item = _item_in_section(service, "Contribution", "Currency")
    assert "CHF" in [o["value"] for o in _options(currency_item)]


def test_trade_symbol_is_terminal_and_currency_is_not(seeded_conn):
    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)

    symbol_options = _options(_item_in_section(service, "Trade", "Symbol"))
    for option in symbol_options:
        if option["value"] == google_forms.NOT_LISTED:
            continue
        assert option["goToAction"] == "SUBMIT_FORM"

    currency_options = _options(_item_in_section(service, "Trade", "Currency"))
    assert all("goToAction" not in option for option in currency_options)


def test_income_symbol_excludes_not_listed_and_currency_is_terminal(seeded_conn):
    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)

    symbol_options = _options(_item_in_section(service, "Income", "Symbol"))
    assert google_forms.NOT_LISTED not in {o["value"] for o in symbol_options}

    currency_options = _options(_item_in_section(service, "Income", "Currency"))
    assert currency_options
    assert all(o["goToAction"] == "SUBMIT_FORM" for o in currency_options)


def test_form_sync_does_not_reintroduce_not_listed_on_income_symbol(seeded_conn, monkeypatch):
    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)
    google_forms._save_form_state({"form_id": service.form_id, "sheet_id": "fake-sheet-id"})
    monkeypatch.setattr(google_forms, "build_forms_service", lambda creds=None: service)

    google_forms.form_sync(seeded_conn)

    symbol_options = _options(_item_in_section(service, "Income", "Symbol"))
    assert google_forms.NOT_LISTED not in {o["value"] for o in symbol_options}


def test_form_sync_preserves_terminal_submit_on_currency(seeded_conn, monkeypatch):
    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)
    google_forms._save_form_state({"form_id": service.form_id, "sheet_id": "fake-sheet-id"})
    monkeypatch.setattr(google_forms, "build_forms_service", lambda creds=None: service)

    google_forms.form_sync(seeded_conn)

    for section_title in ("Income", "Cash", "Cost", "Contribution"):
        currency_options = _options(_item_in_section(service, section_title, "Currency"))
        assert currency_options
        assert all(o["goToAction"] == "SUBMIT_FORM" for o in currency_options), section_title

    trade_currency_options = _options(_item_in_section(service, "Trade", "Currency"))
    assert all("goToAction" not in o for o in trade_currency_options)

    # New instrument's own Currency is no longer terminal — Asset class is.
    new_instrument_currency_options = _options(
        _item_in_section(service, "New instrument", "Currency")
    )
    assert all("goToAction" not in o for o in new_instrument_currency_options)

    trade_symbol_options = _options(_item_in_section(service, "Trade", "Symbol"))
    for option in trade_symbol_options:
        if option["value"] == google_forms.NOT_LISTED:
            continue
        assert option["goToAction"] == "SUBMIT_FORM"


def test_create_form_never_mixes_navigating_and_plain_options_on_one_question(seeded_conn):
    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)
    _assert_no_mixed_navigation(service)


def test_form_sync_never_mixes_navigating_and_plain_options_on_one_question(
    seeded_conn, monkeypatch
):
    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)
    google_forms._save_form_state({"form_id": service.form_id, "sheet_id": "fake-sheet-id"})
    monkeypatch.setattr(google_forms, "build_forms_service", lambda creds=None: service)

    google_forms.form_sync(seeded_conn)
    _assert_no_mixed_navigation(service)


def test_asset_class_routes_to_the_right_subtype_page_or_submits_directly(seeded_conn):
    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)

    def page_break_id(title: str) -> str:
        return next(item["itemId"] for item in service.items if item.get("title") == title)

    asset_class_options = {
        o["value"]: o
        for o in _options(_item_in_section(service, "New instrument", "Asset class"))
    }

    fund_page_id = page_break_id("New instrument — Fund details")
    bond_page_id = page_break_id("New instrument — Bond details")
    crypto_page_id = page_break_id("New instrument — Crypto details")

    assert asset_class_options["FUND"]["goToSectionId"] == fund_page_id
    assert asset_class_options["BOND"]["goToSectionId"] == bond_page_id
    assert asset_class_options["CRYPTO"]["goToSectionId"] == crypto_page_id

    # EQUITY has no subtype table — it submits straight from the landing page.
    assert asset_class_options["EQUITY"]["goToAction"] == "SUBMIT_FORM"
    assert "goToSectionId" not in asset_class_options["EQUITY"]


def test_new_instrument_landing_page_has_only_asset_class_as_navigator(seeded_conn):
    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)

    for label in (
        "Currency", "Protection type", "PEA eligible",
    ):
        options = _options(_item_in_section(service, "New instrument", label))
        assert all(
            "goToAction" not in o and "goToSectionId" not in o for o in options
        ), label

    asset_class_options = _options(_item_in_section(service, "New instrument", "Asset class"))
    assert any("goToAction" in o or "goToSectionId" in o for o in asset_class_options)


def test_fund_bond_crypto_pages_each_have_exactly_one_terminal_field(seeded_conn):
    service = FakeFormsService()
    google_forms.create_form(seeded_conn, service)

    cases = [
        ("New instrument — Fund details", "UCITS",
         ["Instrument type", "Uses securities lending", "SFDR article"]),
        ("New instrument — Bond details", "Is callable",
         ["Coupon frequency", "Issuer type", "Seniority"]),
        ("New instrument — Crypto details", "Is stablecoin", ["Instrument type", "Consensus"]),
    ]
    for section_title, terminal_label, plain_labels in cases:
        terminal_options = _options(_item_in_section(service, section_title, terminal_label))
        assert terminal_options
        assert all(o.get("goToAction") == "SUBMIT_FORM" for o in terminal_options)
        for label in plain_labels:
            plain_options = _options(_item_in_section(service, section_title, label))
            assert all(
                "goToAction" not in o and "goToSectionId" not in o for o in plain_options
            ), (section_title, label)
