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

# Confirmed live: batchUpdate 400s with "ChoiceQuestion.options is
# required" on an empty options list. Only reachable for a symbol field
# that excludes NOT_LISTED (Income) before any instrument/alias exists
# at all — real aliases always push this placeholder out once any
# instrument is configured, and the field stays optional, so nobody is
# forced to actually pick it.
NO_ALIASES_PLACEHOLDER = "(no instrument yet)"


@dataclass(frozen=True)
class Field:
    label: str
    kind: str  # "number" | "text" | "dropdown" | "symbol"
    required: bool = True
    dimension: str | None = None  # for "dropdown": which core.dimensions bucket
    # Confirmed live: a section's page has no "after this page" setting of
    # its own (PageBreakItem carries no navigation at all) — the only
    # navigation primitive is per-option goToAction/goToSectionId on a
    # choice question, and answering none defaults every page to falling
    # through into whichever section was created right after it. Exactly
    # one dropdown/symbol field per section is marked terminal so its
    # non-jump options carry an explicit SUBMIT_FORM, ending the response
    # there instead of cascading into the next unrelated section.
    terminal: bool = False
    # A page can only have one navigating choice-question (confirmed live:
    # a second one's navigation is silently ignored) — so a section whose
    # Symbol field is optional (Income) can't also route "+ Not listed" to
    # New instrument, since Currency must remain that page's sole,
    # unconditional navigator regardless of whether Symbol was answered.
    # Only meaningful for kind="symbol".
    allow_new_instrument: bool = True
    # Fixed dropdown values not tracked in core.dimensions (e.g. a plain
    # true/false), taking precedence over `dimension` when set.
    choices: tuple[str, ...] | None = None
    # dropdown value -> target section KEY (resolved to a section id via
    # section_ids), for a field that branches like Type/Symbol do — e.g.
    # Asset class routing into a Fund/Bond/Crypto-only continuation page.
    # A value with no entry here falls back to `terminal`'s plain
    # SUBMIT_FORM behavior (e.g. EQUITY, which has no subtype page).
    route_by_value: dict[str, str] | None = None


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
            Field("Symbol", "symbol", terminal=True),
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
            Field("Symbol", "symbol", required=False, allow_new_instrument=False),
            Field("Gross", "number"),
            Field("Tax withheld", "number", required=False),
            Field("Currency", "dropdown", dimension="currency", terminal=True),
            Field("Note", "text", required=False),
        ],
    ),
    (
        "cash", "Cash", ["DEPOSIT", "WITHDRAWAL"],
        [
            Field("Amount", "number"),
            Field("Currency", "dropdown", dimension="currency", terminal=True),
            Field("Note", "text", required=False),
        ],
    ),
    (
        "cost", "Cost", ["FEE", "TAX"],
        [
            Field("Amount", "number"),
            Field("Currency", "dropdown", dimension="currency", terminal=True),
            Field("Note", "text", required=False),
        ],
    ),
    (
        "transfer", "Transfer", ["TRANSFER_IN", "TRANSFER_OUT"],
        [
            Field("Symbol", "symbol", terminal=True),
            Field("Quantity", "number"),
            Field("Note", "text", required=False),
        ],
    ),
    (
        "split", "Split", ["SPLIT"],
        [
            Field("Symbol", "symbol", terminal=True),
            Field("New total quantity after the split", "number"),
        ],
    ),
    (
        "staking", "Staking", ["STAKING"],
        [
            Field("Symbol", "symbol", terminal=True),
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
        Field("Currency", "dropdown", dimension="currency"),
        Field("Region", "dropdown", dimension="region", required=False),
        Field("Sector", "dropdown", dimension="sector", required=False),
        Field("Instrument type", "dropdown", dimension="instrument_type", required=False),
        Field("Issuer", "text", required=False),
        Field("Domicile country", "text", required=False),
        Field("Protection type", "dropdown", dimension="protection_type", required=False),
        Field("PEA eligible", "dropdown", choices=("true", "false"), required=False),
        # The page's sole navigator (Currency no longer is) — routes to a
        # subtype-specific continuation page, or straight to submit for an
        # asset class with no subtype table (e.g. EQUITY).
        Field(
            "Asset class", "dropdown", dimension="asset_class", terminal=True,
            route_by_value={
                "ETP": "new_instrument_fund",
                "FUND": "new_instrument_fund",
                "BOND": "new_instrument_bond",
                "CRYPTO": "new_instrument_crypto",
            },
        ),
    ],
)

# Only legal_structure (Fund/ETP) and chain (Crypto) are DB NOT NULL —
# everything else here is optional, straight from config/example/FIELDS.md's
# instruments_fund.csv/instruments_bond.csv/instruments_crypto.csv tables.
# None of these are core.dimensions-tracked (FIELDS.md never marks them
# "*Dimension.*"), so enum-shaped ones use `choices`, not `dimension`.
NEW_INSTRUMENT_FUND_SECTION = (
    "new_instrument_fund", "New instrument — Fund/ETP details", [],
    [
        Field(
            "Legal structure", "dropdown", terminal=True,
            choices=("UCITS_FUND", "NON_UCITS_FUND", "COLLATERALISED_NOTE",
                     "UNSECURED_NOTE", "SCPI"),
        ),
        Field("UCITS", "dropdown", required=False, choices=("true", "false")),
        Field("RHP (years)", "number", required=False),
        Field(
            "Distribution policy", "dropdown", required=False,
            choices=("ACCUMULATING", "DISTRIBUTING"),
        ),
        Field("Ongoing charges", "number", required=False),
        Field("SRI", "number", required=False),
        Field(
            "Replication method", "dropdown", required=False,
            choices=("PHYSICAL_FULL", "PHYSICAL_SAMPLED", "SYNTHETIC"),
        ),
        Field("Swap counterparty", "text", required=False),
        Field("Uses securities lending", "dropdown", required=False, choices=("true", "false")),
        Field("Depositary", "text", required=False),
        Field("SFDR article", "dropdown", required=False, choices=("6", "8", "9")),
        Field("Benchmark index", "text", required=False),
        Field("justETF id", "text", required=False),
        Field("Subscription price (SCPI)", "number", required=False),
        Field("Withdrawal price (SCPI)", "number", required=False),
        Field("Management company (SCPI)", "text", required=False),
        Field("Property sector (SCPI)", "text", required=False),
        Field("Occupancy rate (SCPI)", "number", required=False),
        Field("Distribution rate (SCPI)", "number", required=False),
    ],
)

NEW_INSTRUMENT_BOND_SECTION = (
    "new_instrument_bond", "New instrument — Bond details", [],
    [
        Field("Coupon rate", "number", required=False),
        Field(
            "Coupon frequency", "dropdown", required=False,
            choices=("ANNUAL", "SEMI_ANNUAL", "QUARTERLY", "ZERO"),
        ),
        Field("Maturity date", "date", required=False),
        Field("Face value", "number", required=False),
        Field(
            "Issuer type", "dropdown", required=False,
            choices=("SOVEREIGN", "CORPORATE", "FINANCIAL", "SUPRANATIONAL"),
        ),
        Field("Credit rating", "text", required=False),
        Field(
            "Seniority", "dropdown", required=False,
            choices=("SENIOR_SECURED", "SENIOR_UNSECURED", "SUBORDINATED"),
        ),
        # Forced required (its DB column stays NOT NULL DEFAULT false,
        # unaffected — seed.py already COALESCEs it): nothing else on this
        # page is guaranteed answered, and this isn't the form's last
        # section, so a page with no answered navigator would cascade into
        # whatever section follows — the exact bug class fixed earlier
        # this session, not a hypothetical one.
        Field("Is callable", "dropdown", choices=("true", "false"), terminal=True),
    ],
)

NEW_INSTRUMENT_CRYPTO_SECTION = (
    "new_instrument_crypto", "New instrument — Crypto details", [],
    [
        Field("Chain", "text"),  # DB NOT NULL — required
        Field("Contract address", "text", required=False),
        Field("Token standard", "text", required=False),
        Field("Consensus", "dropdown", required=False, choices=("POW", "POS")),
        Field("Peg currency", "dropdown", required=False, dimension="currency"),
        # Forced required for the same reason as Bond's Is callable above.
        Field("Is stablecoin", "dropdown", choices=("true", "false"), terminal=True),
    ],
)

NEW_INSTRUMENT_SECTIONS = [
    NEW_INSTRUMENT_SECTION,
    NEW_INSTRUMENT_FUND_SECTION,
    NEW_INSTRUMENT_BOND_SECTION,
    NEW_INSTRUMENT_CRYPTO_SECTION,
]

VALUATION_SECTION = (
    "valuation", "Valuation", [VALUATION_TYPE],
    [
        Field("Symbol", "symbol", terminal=True),
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


def symbol_choices(conn: psycopg.Connection, include_not_listed: bool = True) -> list[str]:
    aliases = validate.alias_to_instrument(conn)
    choices = sorted(aliases)
    if include_not_listed:
        return [*choices, NOT_LISTED]
    return choices or [NO_ALIASES_PLACEHOLDER]


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
    return [*SECTIONS, *NEW_INSTRUMENT_SECTIONS, VALUATION_SECTION]


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


def _choice_options(
    field: Field, choices: list[str], goto_section_id_by_value: dict[str, str] | None
) -> list[dict[str, Any]]:
    """Single source of truth for per-option navigation, shared by
    create_form and form_sync so the two paths can't drift out of sync
    with each other the way form_sync's own ad-hoc option-building
    previously did. `goto_section_id_by_value` covers both the single
    NOT_LISTED jump (symbol fields) and a routing field's per-value jump
    (e.g. Asset class -> Fund/Bond/Crypto) — same mechanism, more values."""
    goto_section_id_by_value = goto_section_id_by_value or {}
    options: list[dict[str, Any]] = []
    for choice in choices:
        option: dict[str, Any] = {"value": choice}
        if choice in goto_section_id_by_value:
            option["goToSectionId"] = goto_section_id_by_value[choice]
        elif field.terminal:
            option["goToAction"] = "SUBMIT_FORM"
        options.append(option)
    return options


def _field_request(
    field: Field, index: int, conn: psycopg.Connection, section_ids: dict[str, str]
) -> dict[str, Any]:
    question: dict[str, Any] = {"required": field.required}
    if field.kind in ("text", "number"):
        # No general-purpose validation field exists on TextQuestion
        # (confirmed via discovery-document schema inspection) — numeric
        # checking stays with the loader (step 6), not the Form itself.
        question["textQuestion"] = {}
    elif field.kind == "date":
        question["dateQuestion"] = {"includeYear": True}
    elif field.kind == "dropdown":
        codes = list(field.choices) if field.choices is not None else dimension_codes(
            conn, field.dimension
        )
        goto = (
            {value: section_ids[key] for value, key in field.route_by_value.items()}
            if field.route_by_value
            else None
        )
        question["choiceQuestion"] = {
            "type": "DROP_DOWN",
            "options": _choice_options(field, codes, goto),
        }
    elif field.kind == "symbol":
        choices = symbol_choices(conn, include_not_listed=field.allow_new_instrument)
        goto = {NOT_LISTED: section_ids["new_instrument"]} if field.allow_new_instrument else None
        question["choiceQuestion"] = {
            "type": "DROP_DOWN",
            "options": _choice_options(field, choices, goto),
        }
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

    routing_reply = service.forms().batchUpdate(
        formId=form_id, body={"requests": _routing_requests(conn, section_ids)}
    ).execute()

    # Confirmed live: creating an item at index 0 in a form that already
    # has other items silently drops the title the createItem request
    # asked for (Account and Type, created right after in the same call,
    # keep theirs — only the index-0 slot is affected). A follow-up
    # updateItem restoring the title does stick, so re-assert it here
    # rather than leave every entry with a blank date.
    date_item_id = routing_reply["replies"][0]["createItem"]["itemId"]
    service.forms().batchUpdate(
        formId=form_id,
        body={
            "requests": [
                {
                    "updateItem": {
                        "item": {
                            "itemId": date_item_id,
                            "title": "Date",
                            "questionItem": {
                                "question": {
                                    "required": True,
                                    "dateQuestion": {"includeYear": True},
                                }
                            },
                        },
                        "updateMask": "title",
                        "location": {"index": 0},
                    }
                }
            ]
        },
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
            requests.append(_field_request(field, base_index + i, conn, section_ids))
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
    untouched. Requires form_init to have run first.

    Section-aware (tracks which page each item belongs to) so it can
    rebuild options via the same _choice_options() create_form uses —
    a title match alone can't tell Trade's Symbol (terminal, jumps on
    NOT_LISTED) from Income's (neither), or Trade's Currency (plain)
    from Income's (terminal)."""
    state = load_form_state()
    if state is None:
        raise FormNotInitializedError

    service = build_forms_service()
    form = service.forms().get(formId=state["form_id"]).execute()
    items = form.get("items", [])

    field_by_section = {
        (title, field.label): field
        for _, title, _, fields in _all_sections()
        for field in fields
    }
    new_instrument_section_id = next(
        item["itemId"] for item in items if item.get("title") == "New instrument"
    )

    account_choices = sorted(validate.known_accounts(conn))
    currency_choices = sorted(fx.currencies_from_config())

    requests: list[dict[str, Any]] = []
    current_section_title: str | None = None
    for index, item in enumerate(items):
        if "pageBreakItem" in item:
            current_section_title = item.get("title")
            continue

        question_item = item.get("questionItem")
        if not question_item:
            continue
        choice_question = question_item["question"].get("choiceQuestion")
        if not choice_question:
            continue

        title = item.get("title")
        if title == "Account":
            options = [{"value": a} for a in account_choices]
        elif title in ("Symbol", "Currency"):
            field = field_by_section[(current_section_title, title)]
            if title == "Symbol":
                choices = symbol_choices(conn, include_not_listed=field.allow_new_instrument)
                goto = (
                    {NOT_LISTED: new_instrument_section_id} if field.allow_new_instrument else None
                )
                options = _choice_options(field, choices, goto)
            else:
                options = _choice_options(field, currency_choices, None)
        else:
            continue

        requests.append(_refresh_choice_request(item, options, index))

    if requests:
        service.forms().batchUpdate(
            formId=state["form_id"], body={"requests": requests}
        ).execute()

    return {"form_id": state["form_id"], "updated_items": len(requests)}
