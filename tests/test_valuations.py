from datetime import date, timedelta
from decimal import Decimal

import pytest

from folios.seed import seed
from folios.valuations import ValuationError, load_valuations, manual_priced_instruments
from tests.test_seed import EXAMPLE_CONFIG


@pytest.fixture()
def seeded_conn(migrated_conn):
    seed(migrated_conn, EXAMPLE_CONFIG)
    return migrated_conn


def _write(tmp_path, rows: list[str]):
    path = tmp_path / "valuations.csv"
    path.write_text("date,symbol,price,currency\n" + "\n".join(rows) + "\n")
    return path


def test_load_valuations_stores_into_core_prices(seeded_conn, tmp_path):
    path = _write(tmp_path, ["2026-03-31,FUNDBOND,215.00,EUR"])
    result = load_valuations(seeded_conn, path)
    assert result.rows_read == 1
    assert result.rows_stored == 1

    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT close_price, adj_close, currency, source FROM core.prices "
            "WHERE instrument_id = 'DEMO-FUND-BOND'"
        )
        close_price, adj_close, currency, source = cur.fetchone()
    assert close_price == Decimal("215.00")
    assert adj_close is None
    assert currency == "EUR"
    assert source == "manual"


def test_load_valuations_is_idempotent_upsert(seeded_conn, tmp_path):
    path = _write(tmp_path, ["2026-03-31,FUNDBOND,215.00,EUR"])
    load_valuations(seeded_conn, path)
    load_valuations(seeded_conn, path)

    with seeded_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.prices")
        assert cur.fetchone()[0] == 1

    updated = _write(tmp_path, ["2026-03-31,FUNDBOND,220.00,EUR"])
    load_valuations(seeded_conn, updated)
    with seeded_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.prices")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT close_price FROM core.prices")
        assert cur.fetchone()[0] == Decimal("220.00")


def test_symbol_must_resolve(seeded_conn, tmp_path):
    path = _write(tmp_path, ["2026-03-31,NOT_A_SYMBOL,215.00,EUR"])
    with pytest.raises(ValuationError) as exc_info:
        load_valuations(seeded_conn, path)
    assert "does not resolve" in str(exc_info.value)


def test_instrument_must_be_manual_priced(seeded_conn, tmp_path):
    # DEMO is yfinance-priced, not manual.
    path = _write(tmp_path, ["2026-03-31,DEMO,215.00,EUR"])
    with pytest.raises(ValuationError) as exc_info:
        load_valuations(seeded_conn, path)
    assert "not 'manual'" in str(exc_info.value)


def test_date_must_not_be_in_the_future(seeded_conn, tmp_path):
    future = (date.today() + timedelta(days=1)).isoformat()
    path = _write(tmp_path, [f"{future},FUNDBOND,215.00,EUR"])
    with pytest.raises(ValuationError) as exc_info:
        load_valuations(seeded_conn, path)
    assert "future" in str(exc_info.value)


def test_refuses_to_write_anything_on_any_row_failure(seeded_conn, tmp_path):
    path = _write(
        tmp_path,
        ["2026-03-31,FUNDBOND,215.00,EUR", "2026-03-31,NOT_A_SYMBOL,1,EUR"],
    )
    with pytest.raises(ValuationError):
        load_valuations(seeded_conn, path)

    with seeded_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.prices")
        assert cur.fetchone()[0] == 0


def test_manual_priced_instruments_lists_all_with_last_valuation(seeded_conn, tmp_path):
    path = _write(tmp_path, ["2026-03-31,FUNDBOND,215.00,EUR"])
    load_valuations(seeded_conn, path)

    rows = {r["instrument_id"]: r for r in manual_priced_instruments(seeded_conn)}
    assert rows["DEMO-FUND-BOND"]["last_valued"] == date(2026, 3, 31)
    assert rows["DEMO-BOND-2030"]["last_valued"] is None
