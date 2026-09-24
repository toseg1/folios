from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from folios import fx
from folios.models import compute_txn_hash
from folios.seed import seed
from folios.validate import validate_file
from tests.test_seed import EXAMPLE_CONFIG

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "data" / "samples"


@pytest.fixture()
def seeded_conn(migrated_conn):
    seed(migrated_conn, EXAMPLE_CONFIG)
    return migrated_conn


def test_good_fixture_produces_no_messages(seeded_conn):
    messages = validate_file(seeded_conn, SAMPLES_DIR / "transactions_good.csv")
    assert messages == []


def test_bad_fixture_produces_exactly_one_message_per_planted_error(seeded_conn):
    # The USD row's only planted problem is the currency-mismatch warning —
    # pre-seed a USD rate so it doesn't also trip the FX-missing error.
    fx.store_rates(seeded_conn, {date(2026, 1, 10): {"USD": Decimal("1.10")}})

    messages = validate_file(seeded_conn, SAMPLES_DIR / "transactions_bad.csv")

    assert len(messages) == 9, [str(m) for m in messages]

    by_line = {m.line: m for m in messages}
    assert len(by_line) == 9  # one message per line, none doubled up

    assert "does not exist in core.accounts" in by_line[2].text
    assert by_line[2].level == "error"

    assert "future" in by_line[3].text
    assert by_line[3].level == "error"

    assert "not a known transaction type" in by_line[4].text
    assert by_line[4].level == "error"

    assert "does not resolve" in by_line[5].text
    assert by_line[5].level == "error"

    assert "does not reconcile" in by_line[6].text
    assert by_line[6].level == "error"

    assert "carries its amount in gross" in by_line[7].text
    assert by_line[7].level == "error"

    assert "negative" in by_line[8].text
    assert by_line[8].level == "error"

    assert "no FX rate covers" in by_line[9].text
    assert by_line[9].level == "error"

    assert "does not match" in by_line[10].text
    assert by_line[10].level == "warning"


def test_symbol_suggestion_lists_near_matches(seeded_conn, tmp_path):
    bad_symbol_file = tmp_path / "typo.csv"
    bad_symbol_file.write_text(
        "date,account,type,symbol,quantity,price,gross,fee,tax,currency,note\n"
        "2026-01-10,DEMO-BROKER-CTO,BUY,DEM,1,100,100,0,0,EUR,typo\n"
    )
    messages = validate_file(seeded_conn, bad_symbol_file)
    assert len(messages) == 1
    assert "did you mean" in messages[0].text
    assert "DEMO" in messages[0].text


def test_duplicate_detection_info_messages(seeded_conn, tmp_path):
    entry_file = tmp_path / "dup.csv"
    entry_file.write_text(
        "date,account,type,symbol,quantity,price,gross,fee,tax,currency,note\n"
        "2026-01-10,DEMO-BROKER-CTO,DEPOSIT,,,,100,0,0,EUR,first\n"
    )
    relative = str(entry_file)
    entry_id = f"csv:{relative}:2"

    with seeded_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 net_amount, currency)
            VALUES (%s, 'somehash', 'DEMO-BROKER-CTO', '2026-01-10', 'DEPOSIT',
                    100, 'EUR')
            """,
            (entry_id,),
        )
    seeded_conn.commit()

    messages = validate_file(seeded_conn, entry_file)
    assert len(messages) == 1
    assert messages[0].level == "info"
    assert "changed" in messages[0].text  # txn_hash differs from 'somehash'

    matching_hash = compute_txn_hash(
        {"date": "2026-01-10", "account": "DEMO-BROKER-CTO", "type": "DEPOSIT",
         "symbol": "", "quantity": "", "price": "", "gross": "100", "fee": "0",
         "tax": "0", "currency": "EUR", "note": "first"}
    )
    with seeded_conn.cursor() as cur:
        cur.execute(
            "UPDATE core.transactions SET txn_hash = %s WHERE entry_id = %s",
            (matching_hash, entry_id),
        )
    seeded_conn.commit()

    messages = validate_file(seeded_conn, entry_file)
    assert len(messages) == 1
    assert messages[0].level == "info"
    assert "unchanged" in messages[0].text


def test_general_contribution_needs_a_target_allocation(seeded_conn, tmp_path):
    entry_file = tmp_path / "no_target.csv"
    entry_file.write_text(
        "date,account,type,symbol,quantity,price,gross,fee,tax,currency,note\n"
        "2026-01-10,DEMO-BROKER-CTO,BUY,,,,1000,0,0,EUR,oops no target\n"
    )
    messages = validate_file(seeded_conn, entry_file)
    assert len(messages) == 1
    assert messages[0].level == "error"
    assert "no target allocation" in messages[0].text


def test_general_contribution_needs_every_target_fund_priced(seeded_conn, tmp_path):
    # LINXEA-SPIRIT-AV targets DEMO-ETF-WORLD/DEMO-FONDS-EUROS — neither is
    # priced yet in this fixture, so both should be flagged.
    entry_file = tmp_path / "av.csv"
    entry_file.write_text(
        "date,account,type,symbol,quantity,price,gross,fee,tax,currency,note\n"
        "2026-01-10,LINXEA-SPIRIT-AV,BUY,,,,1000,0,0,EUR,monthly payment\n"
    )
    messages = validate_file(seeded_conn, entry_file)
    errors = [m for m in messages if m.level == "error"]
    assert len(errors) == 2
    assert any("DEMO-ETF-WORLD" in m.text for m in errors)
    assert any("DEMO-FONDS-EUROS" in m.text for m in errors)


def test_running_position_considers_prior_db_transactions(seeded_conn, tmp_path):
    # No BUY anywhere for this instrument yet: even a small SELL must fail.
    entry_file = tmp_path / "sell_only.csv"
    entry_file.write_text(
        "date,account,type,symbol,quantity,price,gross,fee,tax,currency,note\n"
        "2026-03-01,DEMO-BROKER-CTO,SELL,DEMO,1,100,100,0,0,EUR,no prior position\n"
    )
    messages = validate_file(seeded_conn, entry_file)
    assert len(messages) == 1
    assert "negative" in messages[0].text
