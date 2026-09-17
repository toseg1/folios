from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import psycopg
from googleapiclient.discovery import build

from folios import fx, validate
from folios.google.auth import get_credentials
from folios.validate import REPO_ROOT

FORM_STATE_PATH = REPO_ROOT / ".credentials" / "form_state.json"

# Not a real txn_type — routes to the Valuation section only, handled
# entirely by the sheets adapter and `folios value` (step 8b), never
# written to core.transactions. See docs/folios-build-plan.md step 14.
VALUATION_TYPE = "Valuation"

NOT_LISTED = "+ Not listed (new instrument)"


@dataclass(frozen=True)
class Field:
    label: str
    kind: str  # "number" | "text" | "dropdown" | "symbol"
    required: bool = True
    dimension: str | None = None  # for "dropdown": which core.dimensions bucket


# Section key, title, the txn_types that route here, and its fields.
# Matches build-plan step 14's table exactly, with one addition: STAKING
# gets a section too, on the same reasoning the interactive `folios add`
# wizard already gives it its own field profile — the table's own
# omission of it (and of OPENING_BALANCE, which stays CLI/CSV-only as a
# one-time setup action) reads as an intentional scope line for phone
# entry, not an oversight worth second-guessing further than this.
SECTIONS: list[tuple[str, str, list[str], list[Field]]] = [
    (
        "trade", "Trade", ["BUY", "SELL"],
        [
            Field("Symbol", "symbol"),
            Field("Quantity", "number"),
            Field("Price", "number"),
            Field("Gross", "number", required=False),
            Field("Fee", "number", required=False),
            Field("Tax", "number", required=False),
            Field("Currency", "dropdown", dimension="currency"),
            Field("Note", "text", required=False),
        ],
    ),
    (
        "income", "Income", ["DIVIDEND", "INTEREST"],
        [
            Field("Symbol", "symbol", required=False),
            Field("Gross", "number"),
            Field("Tax withheld", "number", required=False),
            Field("Currency", "dropdown", dimension="currency"),
            Field("Note", "text", required=False),
        ],
    ),
    (
        "cash", "Cash", ["DEPOSIT", "WITHDRAWAL"],
        [
            Field("Amount", "number"),
            Field("Currency", "dropdown", dimension="currency"),
            Field("Note", "text", required=False),
        ],
    ),
    (
        "cost", "Cost", ["FEE", "TAX"],
        [
            Field("Amount", "number"),
            Field("Currency", "dropdown", dimension="currency"),
            Field("Note", "text", required=False),
        ],
    ),
    (
        "transfer", "Transfer", ["TRANSFER_IN", "TRANSFER_OUT"],
        [
            Field("Symbol", "symbol"),
            Field("Quantity", "number"),
            Field("Note", "text", required=False),
        ],
    ),
    (
        "split", "Split", ["SPLIT"],
        [
            Field("Symbol", "symbol"),
            Field("New total quantity after the split", "number"),
        ],
    ),
    (
        "staking", "Staking", ["STAKING"],
        [
            Field("Symbol", "symbol"),
            Field("Quantity", "number"),
            Field("Gross (market value at receipt, optional)", "number", required=False),
        ],
    ),
]

NEW_INSTRUMENT_SECTION = (
    "new_instrument", "New instrument", [],
    [
        Field("Name", "text"),
        Field("Yahoo ticker", "text"),
        Field("ISIN", "text", required=False),
        Field("Asset class", "dropdown", dimension="asset_class"),
        Field("Currency", "dropdown", dimension="currency"),
        Field("Region", "dropdown", dimension="region", required=False),
        Field("Sector", "dropdown", dimension="sector", required=False),
    ],
)

VALUATION_SECTION = (
    "valuation", "Valuation", [VALUATION_TYPE],
    [
        Field("Symbol", "symbol"),
        Field("Price", "number"),
        Field("Currency", "dropdown", dimension="currency"),
    ],
)


def dimension_codes(conn: psycopg.Connection, dimension: str) -> list[str]:
    if dimension == "currency":
        return sorted(fx.currencies_from_config())
    with conn.cursor() as cur:
        cur.execute(
            "SELECT code FROM core.dimensions WHERE dimension = %s ORDER BY sort_order, code",
            (dimension,),
        )
        return [row[0] for row in cur.fetchall()]


def symbol_choices(conn: psycopg.Connection) -> list[str]:
    aliases = validate.alias_to_instrument(conn)
    return sorted(aliases) + [NOT_LISTED]


def build_forms_service(creds: Any = None) -> Any:
    return build("forms", "v1", credentials=creds or get_credentials())


def load_form_state() -> dict[str, Any] | None:
    if not FORM_STATE_PATH.exists():
        return None
    return json.loads(FORM_STATE_PATH.read_text())


def _save_form_state(state: dict[str, Any]) -> None:
    FORM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FORM_STATE_PATH.write_text(json.dumps(state, indent=2))


class FormNotInitializedError(Exception):
    def __init__(self) -> None:
        super().__init__("no form_state.json found — run `folios form-init` first.")


FORM_TITLE = "Folios — Transaction Entry"

RESPONSE_SHEET_HEADERS = [
    "response_id", "date", "account", "type", "symbol", "quantity", "price",
    "gross", "fee", "tax", "currency", "note", "status", "message",
]


def _all_sections() -> list[tuple[str, str, list[str], list[Field]]]:
    return [*SECTIONS, NEW_INSTRUMENT_SECTION, VALUATION_SECTION]


def _page_break_requests() -> list[dict[str, Any]]:
    # Pass 1: page breaks only, in final section order — every section
    # needs a real, server-assigned itemId before anything can route to
    # it via goToSectionId.
    return [
        {
            "createItem": {
                "item": {"title": title, "pageBreakItem": {}},
                "location": {"index": i},
            }
        }
        for i, (_, title, _, _) in enumerate(_all_sections())
    ]


def _routing_requests(
    conn: psycopg.Connection, section_ids: dict[str, str]
) -> list[dict[str, Any]]:
    # Pass 2: Date/Account/Type at the front. Type's options carry the
    # goToSectionId values captured from pass 1 — this is the actual
    # branching mechanism (confirmed live: DROP_DOWN choiceQuestion +
    # per-option goToSectionId targeting a pageBreakItem's itemId).
    accounts = sorted(validate.known_accounts(conn))
    type_options: list[dict[str, Any]] = []
    for key, _, txn_types, _ in SECTIONS:
        for txn_type in txn_types:
            type_options.append({"value": txn_type, "goToSectionId": section_ids[key]})
    type_options.append({"value": VALUATION_TYPE, "goToSectionId": section_ids["valuation"]})

    return [
        {
            "createItem": {
                "item": {
                    "title": "Date",
                    "questionItem": {
                        "question": {"required": True, "dateQuestion": {"includeYear": True}}
                    },
                },
                "location": {"index": 0},
            }
        },
        {
            "createItem": {
                "item": {
                    "title": "Account",
                    "questionItem": {
                        "question": {
                            "required": True,
                            "choiceQuestion": {
                                "type": "DROP_DOWN",
                                "options": [{"value": a} for a in accounts],
                            },
                        }
                    },
                },
                "location": {"index": 1},
            }
        },
        {
            "createItem": {
                "item": {
                    "title": "Type",
                    "questionItem": {
                        "question": {
                            "required": True,
                            "choiceQuestion": {"type": "DROP_DOWN", "options": type_options},
                        }
                    },
                },
                "location": {"index": 2},
            }
        },
    ]


def _field_request(
    field: Field, index: int, conn: psycopg.Connection, new_instrument_section_id: str
) -> dict[str, Any]:
    question: dict[str, Any] = {"required": field.required}
    if field.kind in ("text", "number"):
        # No general-purpose validation field exists on TextQuestion
        # (confirmed via discovery-document schema inspection) — numeric
        # checking stays with the loader (step 6), not the Form itself.
        question["textQuestion"] = {}
    elif field.kind == "dropdown":
        assert field.dimension is not None
        question["choiceQuestion"] = {
            "type": "DROP_DOWN",
            "options": [{"value": code} for code in dimension_codes(conn, field.dimension)],
        }
    elif field.kind == "symbol":
        options = []
        for choice in symbol_choices(conn):
            option: dict[str, Any] = {"value": choice}
            if choice == NOT_LISTED:
                option["goToSectionId"] = new_instrument_section_id
            options.append(option)
        question["choiceQuestion"] = {"type": "DROP_DOWN", "options": options}
    else:
        raise ValueError(f"unknown field kind: {field.kind}")

    return {
        "createItem": {
            "item": {"title": field.label, "questionItem": {"question": question}},
            "location": {"index": index},
        }
    }


def create_form(conn: psycopg.Connection, service: Any) -> dict[str, Any]:
    """Builds the branching entry Form from config/ + core.dimensions, via
    a 3-pass batchUpdate strategy — required because an option's
    goToSectionId must reference a page break's itemId, which only exists
    once that page break has actually been created server-side. See
    docs/folios-build-plan.md step 14."""
    created = (
        service.forms()
        .create(body={"info": {"title": FORM_TITLE, "documentTitle": FORM_TITLE}})
        .execute()
    )
    form_id = created["formId"]

    reply = service.forms().batchUpdate(
        formId=form_id, body={"requests": _page_break_requests()}
    ).execute()
    section_ids = {
        section[0]: r["createItem"]["itemId"]
        for section, r in zip(_all_sections(), reply["replies"], strict=True)
    }

    service.forms().batchUpdate(
        formId=form_id, body={"requests": _routing_requests(conn, section_ids)}
    ).execute()

    # Pass 3: refetch for each page break's post-shift index, then insert
    # each section's fields right after its own page break. All of this
    # section's insertions happen within one batchUpdate call, so a
    # running offset accounts for earlier sections' insertions shifting
    # every index after them.
    form = service.forms().get(formId=form_id).execute()
    index_by_item_id = {item["itemId"]: i for i, item in enumerate(form["items"])}

    requests: list[dict[str, Any]] = []
    offset = 0
    for key, _, _, fields in _all_sections():
        base_index = index_by_item_id[section_ids[key]] + 1 + offset
        for i, field in enumerate(fields):
            requests.append(
                _field_request(field, base_index + i, conn, section_ids["new_instrument"])
            )
        offset += len(fields)

    if requests:
        service.forms().batchUpdate(formId=form_id, body={"requests": requests}).execute()

    return {"form_id": form_id, "responder_uri": form.get("responderUri")}


def build_sheets_service(creds: Any = None) -> Any:
    return build("sheets", "v4", credentials=creds or get_credentials())


def create_response_sheet(sheets_service: Any, title: str) -> str:
    """The Forms REST API has no way to programmatically link a response
    spreadsheet (confirmed live — no destination field on a created
    form, no such method on forms.forms()). So folios makes its own
    Sheet and populates it itself from forms.responses.list() — see the
    sheets adapter, step 15."""
    spreadsheet = (
        sheets_service.spreadsheets()
        .create(
            body={
                "properties": {"title": title},
                "sheets": [
                    {
                        "properties": {"title": "Responses"},
                        "data": [
                            {
                                "startRow": 0,
                                "startColumn": 0,
                                "rowData": [
                                    {
                                        "values": [
                                            {"userEnteredValue": {"stringValue": h}}
                                            for h in RESPONSE_SHEET_HEADERS
                                        ]
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        .execute()
    )
    return spreadsheet["spreadsheetId"]


def form_init(conn: psycopg.Connection) -> dict[str, Any]:
    """Builds a fresh Form + folios-managed response Sheet from config/,
    and saves both ids to .credentials/form_state.json. Re-running this
    creates a brand new Form/Sheet pair — use form_sync to refresh an
    existing form's dropdowns in place instead."""
    creds = get_credentials()
    forms_service = build_forms_service(creds)
    sheets_service = build_sheets_service(creds)

    result = create_form(conn, forms_service)
    sheet_id = create_response_sheet(sheets_service, f"{FORM_TITLE} — Responses")

    state = {
        "form_id": result["form_id"],
        "responder_uri": result.get("responder_uri"),
        "sheet_id": sheet_id,
    }
    _save_form_state(state)
    return state


def _refresh_choice_request(
    item: dict[str, Any], options: list[dict[str, Any]], index: int
) -> dict[str, Any]:
    question = item["questionItem"]["question"]
    choice = dict(question["choiceQuestion"])
    choice["options"] = options
    return {
        "updateItem": {
            "item": {
                "itemId": item["itemId"],
                "title": item["title"],
                "questionItem": {"question": {**question, "choiceQuestion": choice}},
            },
            "location": {"index": index},
            "updateMask": "questionItem.question.choiceQuestion.options",
        }
    }


def form_sync(conn: psycopg.Connection) -> dict[str, Any]:
    """Refreshes the Account/Symbol/Currency dropdown choices on the
    existing form in place — structure, page breaks and routing are
    untouched. Requires form_init to have run first."""
    state = load_form_state()
    if state is None:
        raise FormNotInitializedError

    service = build_forms_service()
    form = service.forms().get(formId=state["form_id"]).execute()

    account_choices = sorted(validate.known_accounts(conn))
    new_symbol_choices = symbol_choices(conn)
    currency_choices = sorted(fx.currencies_from_config())

    requests: list[dict[str, Any]] = []
    for index, item in enumerate(form.get("items", [])):
        question_item = item.get("questionItem")
        if not question_item:
            continue
        choice_question = question_item["question"].get("choiceQuestion")
        if not choice_question:
            continue

        title = item.get("title")
        if title == "Account":
            options = [{"value": a} for a in account_choices]
        elif title == "Symbol":
            options = []
            for choice in new_symbol_choices:
                option: dict[str, Any] = {"value": choice}
                if choice == NOT_LISTED:
                    existing = next(
                        (o for o in choice_question["options"] if o.get("value") == NOT_LISTED),
                        None,
                    )
                    if existing and "goToSectionId" in existing:
                        option["goToSectionId"] = existing["goToSectionId"]
                options.append(option)
        elif title == "Currency":
            options = [{"value": c} for c in currency_choices]
        else:
            continue

        requests.append(_refresh_choice_request(item, options, index))

    if requests:
        service.forms().batchUpdate(
            formId=state["form_id"], body={"requests": requests}
        ).execute()

    return {"form_id": state["form_id"], "updated_items": len(requests)}
