from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from folios import fx
from folios.loader import LoadValidationError, load_all, load_file, rebuild
from folios.seed import seed
from tests.test_seed import EXAMPLE_CONFIG

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "data" / "samples"
GOOD_FIXTURE = SAMPLES_DIR / "transactions_good.csv"


@pytest.fixture()
def seeded_conn(migrated_conn):
    seed(migrated_conn, EXAMPLE_CONFIG)
    return migrated_conn


def test_loading_good_fixture_inserts_every_row(seeded_conn):
    result = load_file(seeded_conn, GOOD_FIXTURE)
    assert result.rows_read == 5
    assert result.rows_inserted == 5
    assert result.rows_updated == 0
    assert result.rows_skipped == 0

    with seeded_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.transactions")
        assert cur.fetchone()[0] == 5


def test_loading_again_inserts_nothing_and_skips_all(seeded_conn):
    load_file(seeded_conn, GOOD_FIXTURE)
    result = load_file(seeded_conn, GOOD_FIXTURE)

    assert result.rows_inserted == 0
    assert result.rows_updated == 0
    assert result.rows_skipped == 5

    with seeded_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.transactions")
        assert cur.fetchone()[0] == 5  # no duplicates


def test_editing_one_field_updates_only_that_row_and_reports_the_field(
    seeded_conn, tmp_path
):
    edited = tmp_path / "transactions_good.csv"
    edited.write_text(GOOD_FIXTURE.read_text())
    load_file(seeded_conn, edited)

    original = edited.read_text()
    # Row 4 (line 5, the FEE row): gross 5 -> 7.50
    edited.write_text(original.replace(",FEE,,,,5,0,0,EUR,", ",FEE,,,,7.50,0,0,EUR,"))

    result = load_file(seeded_conn, edited)

    assert result.rows_inserted == 0
    assert result.rows_updated == 1
    assert result.rows_skipped == 4

    entry_id = f"csv:{edited}:5"
    assert result.updated_fields[entry_id] == ["gross_amount", "net_amount"]

    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT gross_amount, net_amount FROM core.transactions WHERE entry_id = %s",
            (entry_id,),
        )
        gross_amount, net_amount = cur.fetchone()
        assert gross_amount == Decimal("7.50")
        assert net_amount == Decimal("-7.50")

        cur.execute("SELECT count(*) FROM core.transactions")
        assert cur.fetchone()[0] == 5  # updated in place, not duplicated


def test_load_refuses_to_insert_anything_on_any_row_failure(seeded_conn):
    with pytest.raises(LoadValidationError):
        load_file(seeded_conn, SAMPLES_DIR / "transactions_bad.csv")

    with seeded_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.transactions")
        assert cur.fetchone()[0] == 0


def test_load_records_a_load_run(seeded_conn):
    load_file(seeded_conn, GOOD_FIXTURE)
    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT status, rows_inserted FROM core.load_runs ORDER BY run_id DESC LIMIT 1"
        )
        status, rows_inserted = cur.fetchone()
    assert status == "success"
    assert rows_inserted == 5


def test_fx_rate_is_frozen_onto_the_row(seeded_conn, tmp_path):
    fx.store_rates(seeded_conn, {date(2026, 1, 5): {"USD": Decimal("1.10")}})
    usd_file = tmp_path / "usd.csv"
    usd_file.write_text(
        "date,account,type,symbol,quantity,price,gross,fee,tax,currency,note\n"
        "2026-01-05,DEMO-BROKER-CTO,BUY,DEMO,1,100,100,0,0,USD,\n"
    )
    load_file(seeded_conn, usd_file)

    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT fx_rate_to_base, fx_rate_date FROM core.transactions "
            "WHERE currency = 'USD'"
        )
        fx_rate_to_base, fx_rate_date = cur.fetchone()
    assert fx_rate_to_base == Decimal("1.10")
    assert fx_rate_date == date(2026, 1, 5)


def test_rebuild_reproduces_entry_ids_and_sums(migrated_conn, tmp_path):
    data_dir = tmp_path / "manual"
    data_dir.mkdir()
    (data_dir / "transactions.csv").write_text(GOOD_FIXTURE.read_text())

    rebuild(migrated_conn, data_dir=data_dir, config_dir=EXAMPLE_CONFIG)

    def snapshot():
        with migrated_conn.cursor() as cur:
            cur.execute("SELECT entry_id FROM core.transactions ORDER BY entry_id")
            entry_ids = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT account_id, sum(quantity), sum(net_amount) "
                "FROM core.transactions GROUP BY account_id ORDER BY account_id"
            )
            sums = cur.fetchall()
        return entry_ids, sums

    before = snapshot()
    rebuild(migrated_conn, data_dir=data_dir, config_dir=EXAMPLE_CONFIG)
    after = snapshot()

    assert before == after


def test_rebuild_ignores_valuation_files_in_the_same_directory(migrated_conn, tmp_path):
    # valuations.csv and gform_valuations_*.csv share data/manual/ with
    # the transaction files but have a completely different column
    # shape — rebuild must not try to load them as transactions.
    data_dir = tmp_path / "manual"
    data_dir.mkdir()
    (data_dir / "transactions.csv").write_text(GOOD_FIXTURE.read_text())
    (data_dir / "valuations.csv").write_text(
        "date,symbol,price,currency\n2026-01-10,FUNDBOND,105,EUR\n"
    )
    (data_dir / "gform_valuations_2026-01-10.csv").write_text(
        "date,symbol,price,currency\n2026-01-11,BOND2030,101,EUR\n"
    )

    result = rebuild(migrated_conn, data_dir=data_dir, config_dir=EXAMPLE_CONFIG)

    assert result.rows_read == 5
    with migrated_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.transactions")
        assert cur.fetchone() == (5,)


def test_load_all_aggregates_across_files_and_skips_valuations(migrated_conn, tmp_path):
    data_dir = tmp_path / "manual"
    data_dir.mkdir()
    (data_dir / "transactions.csv").write_text(GOOD_FIXTURE.read_text())
    (data_dir / "gform_2026-01-15.csv").write_text(
        "date,account,type,symbol,quantity,price,gross,fee,tax,currency,note\n"
        "2026-01-15,DEMO-BROKER-CTO,DEPOSIT,,,,500,0,0,EUR,phone deposit\n"
    )
    (data_dir / "valuations.csv").write_text(
        "date,symbol,price,currency\n2026-01-10,FUNDBOND,105,EUR\n"
    )
    seed(migrated_conn, EXAMPLE_CONFIG)

    result, errors = load_all(migrated_conn, data_dir=data_dir)

    assert errors == []
    assert result.rows_read == 6
    assert result.rows_inserted == 6


def test_load_all_continues_past_a_broken_file(migrated_conn, tmp_path):
    data_dir = tmp_path / "manual"
    data_dir.mkdir()
    (data_dir / "transactions.csv").write_text(GOOD_FIXTURE.read_text())
    (data_dir / "gform_2026-01-15.csv").write_text(
        "date,account,type,symbol,quantity,price,gross,fee,tax,currency,note\n"
        "2026-01-15,NOT-A-REAL-ACCOUNT,DEPOSIT,,,,500,0,0,EUR,bad row\n"
    )
    seed(migrated_conn, EXAMPLE_CONFIG)

    result, errors = load_all(migrated_conn, data_dir=data_dir)

    # The good file still loaded in full, despite the broken one.
    assert result.rows_inserted == 5
    assert len(errors) == 1
    assert "NOT-A-REAL-ACCOUNT" in errors[0]
