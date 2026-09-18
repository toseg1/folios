from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
from googleapiclient.discovery import build

from folios import new_instrument, validate
from folios.google.auth import get_credentials
from folios.google.forms import (
    NEW_INSTRUMENT_SECTIONS,
    NOT_LISTED,
    RESPONSE_SHEET_HEADERS,
    SECTIONS,
    VALUATION_TYPE,
    FormNotInitializedError,
    build_forms_service,
    load_form_state,
)
from folios.models import ENTRY_CSV_COLUMNS
from folios.validate import REPO_ROOT
from folios.valuations import VALUATION_CSV_COLUMNS

PULL_STATE_PATH = REPO_ROOT / ".credentials" / "pull_state.json"
MANUAL_DIR = REPO_ROOT / "data" / "manual"

# _sheet_row builds rows in exactly this order by hand — assert it so the
# two files can't silently drift apart.
assert RESPONSE_SHEET_HEADERS == ["response_id", *ENTRY_CSV_COLUMNS, "status", "message"]

# Question label -> transaction CSV column. Built once from every section
# in forms.py's SECTIONS table (step 14) — a label only ever appears in
# one section's fields with one consistent meaning, except where noted.
_FIELD_TO_COLUMN: dict[str, str] = {
    "Symbol": "symbol",
    "Quantity": "quantity",
    "New total quantity after the split": "quantity",
    "Price": "price",
    "Gross": "gross",
    "Gross (market value at receipt, optional)": "gross",
    "Amount": "gross",  # Cash/Cost sections have no separate quantity/price
    "Fee": "fee",
    "Tax": "tax",
    "Tax withheld": "tax",
    "Currency": "currency",
    "Note": "note",
}

# Transfer/Split/Staking have no Currency field of their own (the build
# plan's own step-14 table omits it too) — the instrument only has one
# currency anyway, so pull fills it in from core.instruments, exactly
# like the `folios add` wizard already defaults it. See forms.py SECTIONS.
_SECTIONS_NEEDING_CURRENCY_FROM_INSTRUMENT = {"transfer", "split", "staking"}

# All four New-Instrument-family pages (the landing page plus the
# Fund/Bond/Crypto continuation pages) — a response can carry answers
# from the landing page AND whichever continuation page it was routed
# to, and all of those belong in new_instrument_fields, not fields.
_NEW_INSTRUMENT_SECTION_TITLES = {title for _, title, _, _ in NEW_INSTRUMENT_SECTIONS}


def build_sheets_service(creds: Any = None) -> Any:
    return build("sheets", "v4", credentials=creds or get_credentials())


def load_pull_state() -> dict[str, Any]:
    if not PULL_STATE_PATH.exists():
        return {"processed_response_ids": []}
    return json.loads(PULL_STATE_PATH.read_text())


def _save_pull_state(state: dict[str, Any]) -> None:
    PULL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PULL_STATE_PATH.write_text(json.dumps(state, indent=2))


@dataclass
class PullResult:
    responses_seen: int = 0
    transactions_written: int = 0
    valuations_written: int = 0
    new_instruments: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _section_for_type(txn_type: str) -> str | None:
    for key, _, txn_types, _ in SECTIONS:
        if txn_type in txn_types:
            return key
    return None


def _question_section_and_title(form: dict[str, Any]) -> dict[str, tuple[str | None, str]]:
    """Maps questionId -> (section title, field title). Section-aware
    because a "+ Not listed" jump means a single response can carry
    answers from two different pages at once — the original section
    plus New instrument — that happen to share a field label (both have
    a "Currency" question). A flat title-only map would let whichever
    one iterates last silently clobber the other's answer."""
    meta: dict[str, tuple[str | None, str]] = {}
    current_section: str | None = None
    for item in form.get("items", []):
        if "pageBreakItem" in item:
            current_section = item.get("title")
            continue
        question = item.get("questionItem", {}).get("question")
        if question and "questionId" in question and "title" in item:
            meta[question["questionId"]] = (current_section, item["title"])
    return meta


def _answer_text(answer: dict[str, Any]) -> str:
    # Forms API v1 represents every question kind used here — text,
    # number, date (with includeYear: true -> "YYYY-MM-DD"), and choice
    # (dropdown) — uniformly as textAnswers.answers[].value. Confirmed
    # against the live discovery document, not assumed.
    values = [a["value"] for a in answer.get("textAnswers", {}).get("answers", [])]
    return ", ".join(values)


def _response_fields(
    response: dict[str, Any], question_meta: dict[str, tuple[str | None, str]]
) -> tuple[dict[str, str], dict[str, str]]:
    """Splits a response's answers by which page they came from (see
    _question_section_and_title). Returns (fields, new_instrument_fields):
    fields holds every answer except New instrument's own — New
    instrument's are kept separate so its "Currency" (the instrument's
    own) can never be confused with the originating section's."""
    fields: dict[str, str] = {}
    new_instrument_fields: dict[str, str] = {}
    for question_id, answer in response.get("answers", {}).items():
        meta = question_meta.get(question_id)
        if meta is None:
            continue
        section, title = meta
        target = (
            new_instrument_fields if section in _NEW_INSTRUMENT_SECTION_TITLES else fields
        )
        target[title] = _answer_text(answer)
    return fields, new_instrument_fields


def _list_all_responses(forms_service: Any, form_id: str) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    page_token = None
    while True:
        result = (
            forms_service.forms()
            .responses()
            .list(formId=form_id, pageToken=page_token)
            .execute()
        )
        responses.extend(result.get("responses", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return responses


def _build_transaction_row(
    fields: dict[str, str],
    txn_type: str,
    section_key: str,
    aliases: dict[str, str],
    instrument_currencies: dict[str, str],
) -> dict[str, str]:
    row = dict.fromkeys(ENTRY_CSV_COLUMNS, "")
    row["date"] = fields.get("Date", "")
    row["account"] = fields.get("Account", "")
    row["type"] = txn_type

    for label, value in fields.items():
        column = _FIELD_TO_COLUMN.get(label)
        if column:
            row[column] = value

    if section_key in _SECTIONS_NEEDING_CURRENCY_FROM_INSTRUMENT and not row["currency"]:
        instrument_id = aliases.get(row["symbol"])
        if instrument_id:
            row["currency"] = instrument_currencies.get(instrument_id, "")

    row["fee"] = row["fee"] or "0"
    row["tax"] = row["tax"] or "0"
    return row


def _sheet_row(response_id: str, fields: dict[str, str], status: str, message: str) -> list[str]:
    row = dict.fromkeys(ENTRY_CSV_COLUMNS, "")
    row["date"] = fields.get("Date", "")
    row["account"] = fields.get("Account", "")
    row["type"] = fields.get("Type", "")
    row["symbol"] = fields.get("Symbol", "")
    for label, value in fields.items():
        column = _FIELD_TO_COLUMN.get(label)
        if column:
            row[column] = value
    return [response_id, *(row[c] for c in ENTRY_CSV_COLUMNS), status, message]


def _append_csv_rows(
    path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]
) -> list[int]:
    """Appends rows to path (creating it with a header first if needed),
    never touching existing lines — this is what keeps a pulled row's
    entry_id (csv:<path>:<line>) stable across repeated pulls. Returns
    each new row's 1-indexed line number, matching validate.read_rows'
    convention (header is line 1)."""
    if not rows:
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists()
    if file_exists:
        with path.open(newline="", encoding="utf-8") as f:
            line_count = sum(1 for _ in f)
    else:
        line_count = 0

    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        if not file_exists:
            writer.writeheader()
            line_count = 1
        for row in rows:
            writer.writerow(row)
            line_count += 1

    first_line = line_count - len(rows) + 1
    return list(range(first_line, line_count + 1))


def _append_sheet_rows(sheets_service: Any, sheet_id: str, rows: list[list[str]]) -> None:
    sheets_service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range="Responses!A:A",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def pull_responses(
    conn: psycopg.Connection,
    forms_service: Any,
    sheets_service: Any,
    today: date | None = None,
) -> PullResult:
    """Reads every new Form response, writes transactions to
    data/manual/gform_<today>.csv, Valuation-typed rows to
    data/manual/gform_valuations_<today>.csv, and writes status/message
    for every response back into the response Sheet — using
    `validate.validate_file` (the exact machinery `folios validate` uses)
    so a broken submission is flagged with the same message a human would
    get from the CLI. Never writes to core.transactions itself: the Sheet
    and these files are a transport, the loader is the only writer of
    record. See docs/folios-build-plan.md step 15.

    A "not listed" Symbol answer registers the new instrument first
    (build-plan step 16: validate the ticker, add it to config/,
    reconcile into the database via seed.seed()), then — since a Form
    response carries every answer from the whole visit, not just the
    New instrument page's — falls through and completes the very
    transaction/valuation the respondent was mid-way through entering,
    now that the symbol resolves to the freshly created alias."""
    state = load_form_state()
    if state is None:
        raise FormNotInitializedError

    pull_state = load_pull_state()
    processed_ids = set(pull_state.get("processed_response_ids", []))

    form = forms_service.forms().get(formId=state["form_id"]).execute()
    question_meta = _question_section_and_title(form)

    responses = _list_all_responses(forms_service, state["form_id"])
    new_responses = [r for r in responses if r["responseId"] not in processed_ids]

    result = PullResult(responses_seen=len(new_responses))
    if not new_responses:
        return result

    fields_by_response: dict[str, dict[str, str]] = {}
    new_instrument_fields_by_response: dict[str, dict[str, str]] = {}
    for r in new_responses:
        fields, new_instrument_fields = _response_fields(r, question_meta)
        fields_by_response[r["responseId"]] = fields
        new_instrument_fields_by_response[r["responseId"]] = new_instrument_fields
    aliases = validate.alias_to_instrument(conn)
    instrument_currencies = validate.instrument_currencies(conn)

    today = today or date.today()
    txn_path = MANUAL_DIR / f"gform_{today.isoformat()}.csv"
    valuation_path = MANUAL_DIR / f"gform_valuations_{today.isoformat()}.csv"

    txn_batch: list[tuple[str, dict[str, str]]] = []
    valuation_batch: list[tuple[str, dict[str, str]]] = []
    status_by_response: dict[str, tuple[str, str]] = {}
    instrument_messages: dict[str, str] = {}

    for response_id, fields in fields_by_response.items():
        txn_type = fields.get("Type", "")
        symbol = fields.get("Symbol", "")

        if symbol == NOT_LISTED:
            new_instrument_fields = new_instrument_fields_by_response[response_id]
            outcome = new_instrument.create_from_form_fields(conn, new_instrument_fields)
            if outcome.error:
                status_by_response[response_id] = ("error", outcome.error)
                result.errors.append(f"{response_id}: {outcome.error}")
                continue

            result.new_instruments.append(outcome.instrument_id)
            instrument_messages[response_id] = (
                f"created {outcome.instrument_id} (alias {outcome.alias})"
            )
            aliases = validate.alias_to_instrument(conn)
            instrument_currencies = validate.instrument_currencies(conn)
            symbol = outcome.alias
            fields = {**fields, "Symbol": symbol}
            fields_by_response[response_id] = fields

        if txn_type == VALUATION_TYPE:
            valuation_batch.append(
                (
                    response_id,
                    {
                        "date": fields.get("Date", ""),
                        "symbol": symbol,
                        "price": fields.get("Price", ""),
                        "currency": fields.get("Currency", ""),
                    },
                )
            )
            continue

        section_key = _section_for_type(txn_type)
        if section_key is None:
            message = f"type={txn_type!r} does not resolve to a known section"
            status_by_response[response_id] = ("error", message)
            result.errors.append(f"{response_id}: {message}")
            continue

        row = _build_transaction_row(fields, txn_type, section_key, aliases, instrument_currencies)
        txn_batch.append((response_id, row))

    txn_lines = _append_csv_rows(txn_path, ENTRY_CSV_COLUMNS, [row for _, row in txn_batch])
    _append_csv_rows(valuation_path, VALUATION_CSV_COLUMNS, [row for _, row in valuation_batch])

    if txn_batch:
        messages = validate.validate_file(conn, txn_path)
        errors_by_line: dict[int, list[str]] = {}
        for message in messages:
            if message.level == "error":
                errors_by_line.setdefault(message.line, []).append(message.text)

        for (response_id, _row), line in zip(txn_batch, txn_lines, strict=True):
            row_errors = errors_by_line.get(line)
            prefix = instrument_messages.get(response_id)
            if row_errors:
                text = "; ".join(row_errors)
                if prefix:
                    text = f"{prefix}; {text}"
                status_by_response[response_id] = ("error", text)
                result.errors.append(f"{response_id}: {text}")
            else:
                status_by_response[response_id] = ("ok", prefix or "")
                result.transactions_written += 1

    for response_id, _row in valuation_batch:
        prefix = instrument_messages.get(response_id)
        status_by_response[response_id] = ("ok", prefix or "")
        result.valuations_written += 1

    sheet_rows = [
        _sheet_row(response_id, fields, *status_by_response.get(response_id, ("ok", "")))
        for response_id, fields in fields_by_response.items()
    ]
    _append_sheet_rows(sheets_service, state["sheet_id"], sheet_rows)

    processed_ids.update(fields_by_response.keys())
    _save_pull_state({"processed_response_ids": sorted(processed_ids)})

    return result


def pull(conn: psycopg.Connection) -> PullResult:
    creds = get_credentials()
    forms_service = build_forms_service(creds)
    sheets_service = build_sheets_service(creds)
    return pull_responses(conn, forms_service, sheets_service)
