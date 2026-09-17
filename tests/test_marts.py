from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from folios.loader import load_file
from folios.seed import seed
from tests.test_seed import EXAMPLE_CONFIG

GOOD_FIXTURE = Path(__file__).resolve().parents[1] / "data" / "samples" / "transactions_good.csv"


@pytest.fixture()
def seeded_conn(migrated_conn):
    seed(migrated_conn, EXAMPLE_CONFIG)
    return migrated_conn


@pytest.fixture()
def loaded_conn(seeded_conn):
    """The good fixture loaded, plus a close price so v_positions has a
    market value. Worked-example numbers (hand-computed):
    BUY 10@100 fee1 -> net -1001; DIVIDEND 9.60-1.44 -> net 8.16;
    DEPOSIT 2000 -> net 2000; FEE 5 -> net -5; SELL 5@110 fee1 -> net 549.
    Ending position: 5 shares. Ending cash (DEMO-BROKER-CTO, EUR):
    -1001 + 8.16 - 5 + 549 = -448.84. DEMO-BANK-CURRENT: 2000.
    """
    load_file(seeded_conn, GOOD_FIXTURE)
    with seeded_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.prices "
            "(instrument_id, price_date, close_price, adj_close, currency, source) "
            "VALUES ('DEMO-SHARE', '2026-02-20', 120.00, 120.00, 'EUR', 'yfinance')"
        )
    seeded_conn.commit()
    return seeded_conn


def _rows(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def test_v_positions_matches_hand_computed_result(loaded_conn):
    rows = _rows(loaded_conn, "SELECT * FROM marts.v_positions")
    assert len(rows) == 1
    row = rows[0]
    assert row["account_id"] == "DEMO-BROKER-CTO"
    assert row["instrument_id"] == "DEMO-SHARE"
    assert row["quantity"] == Decimal("5")
    assert row["latest_price"] == Decimal("120.00000000")
    assert row["market_value_base"] == Decimal("600.00000000000000000000000000")


def test_v_cash_balances_matches_hand_computed_result(loaded_conn):
    rows = {
        r["account_id"]: r["balance"]
        for r in _rows(loaded_conn, "SELECT * FROM marts.v_cash_balances")
    }
    assert rows["DEMO-BROKER-CTO"] == Decimal("-448.8400")
    assert rows["DEMO-BANK-CURRENT"] == Decimal("2000.0000")


def test_v_allocation_totals_equal_positions_plus_cash(loaded_conn):
    total_alloc = _rows(loaded_conn, "SELECT sum(value_base) AS t FROM marts.v_allocation")[0]["t"]
    total_pos = _rows(
        loaded_conn, "SELECT sum(market_value_base) AS t FROM marts.v_positions"
    )[0]["t"]
    total_cash = _rows(
        loaded_conn, "SELECT sum(balance) AS t FROM marts.v_cash_balances"
    )[0]["t"]
    assert total_alloc == total_pos + total_cash


def test_v_allocation_cash_rows_have_no_instrument_dimensions(loaded_conn):
    rows = _rows(
        loaded_conn, "SELECT * FROM marts.v_allocation WHERE asset_class = 'CASH'"
    )
    assert len(rows) == 2
    for row in rows:
        assert row["instrument_id"] is None
        assert row["sector"] is None


def test_v_positions_daily_has_no_gaps(loaded_conn):
    bounds = _rows(
        loaded_conn,
        "SELECT count(*) AS n, min(as_of_date) AS lo, max(as_of_date) AS hi "
        "FROM marts.v_positions_daily",
    )[0]
    expected_days = (bounds["hi"] - bounds["lo"]).days + 1
    assert bounds["n"] == expected_days
    assert bounds["lo"] == date(2026, 1, 5)  # first trade date
    assert bounds["hi"] == date.today()


def test_v_positions_daily_tracks_quantity_changes(loaded_conn):
    def qty_on(d):
        return _rows(
            loaded_conn,
            "SELECT quantity FROM marts.v_positions_daily WHERE as_of_date = %s",
            (d,),
        )[0]["quantity"]

    assert qty_on(date(2026, 1, 5)) == Decimal("10")  # BUY day
    assert qty_on(date(2026, 2, 14)) == Decimal("10")  # day before SELL
    assert qty_on(date(2026, 2, 15)) == Decimal("5")  # SELL day
    assert qty_on(date.today()) == Decimal("5")


def test_v_positions_daily_forward_fills_price(loaded_conn):
    rows = {
        r["as_of_date"]: r["close_price"]
        for r in _rows(
            loaded_conn,
            "SELECT as_of_date, close_price FROM marts.v_positions_daily "
            "WHERE as_of_date IN (%s, %s, %s)",
            (date(2026, 1, 5), date(2026, 2, 20), date(2026, 2, 21)),
        )
    }
    assert rows[date(2026, 1, 5)] is None  # before any price ever existed
    assert rows[date(2026, 2, 20)] == Decimal("120.00000000")  # the priced day
    assert rows[date(2026, 2, 21)] == Decimal("120.00000000")  # forward-filled


def test_v_costs_sums_embedded_and_standalone_and_excludes_income_withholding(
    loaded_conn,
):
    rows = {
        (r["month"], r["account_id"]): r
        for r in _rows(loaded_conn, "SELECT * FROM marts.v_costs")
    }
    jan = rows[(date(2026, 1, 1), "DEMO-BROKER-CTO")]
    assert jan["embedded_costs"] == Decimal("1.0000")  # BUY fee only
    assert jan["standalone_costs"] == Decimal("0")
    # DIVIDEND's tax=1.44 withholding must not appear here at all.
    assert jan["total_costs"] == Decimal("1.0000")

    feb = rows[(date(2026, 2, 1), "DEMO-BROKER-CTO")]
    assert feb["embedded_costs"] == Decimal("1.0000")  # SELL fee
    assert feb["standalone_costs"] == Decimal("5.0000")  # standalone FEE row
    assert feb["total_costs"] == Decimal("6.0000")


def test_v_reconciliation_empty_when_no_statements(loaded_conn):
    assert _rows(loaded_conn, "SELECT * FROM marts.v_reconciliation") == []


def test_v_reconciliation_flags_position_discrepancy(loaded_conn):
    with loaded_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.statement_positions "
            "(account_id, as_of, instrument_id, quantity) "
            "VALUES ('DEMO-BROKER-CTO', '2026-02-20', 'DEMO-SHARE', 999)"
        )
    loaded_conn.commit()

    rows = _rows(loaded_conn, "SELECT * FROM marts.v_reconciliation WHERE leg = 'position'")
    assert len(rows) == 1
    assert rows[0]["statement_value"] == Decimal("999")
    assert rows[0]["derived_value"] == Decimal("5")
    assert rows[0]["diff"] == Decimal("994")


def test_v_reconciliation_flags_cash_discrepancy(loaded_conn):
    with loaded_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.statement_cash (account_id, as_of, currency, balance) "
            "VALUES ('DEMO-BANK-CURRENT', '2026-02-01', 'EUR', 1500)"
        )
    loaded_conn.commit()

    rows = _rows(loaded_conn, "SELECT * FROM marts.v_reconciliation WHERE leg = 'cash'")
    assert len(rows) == 1
    assert rows[0]["statement_value"] == Decimal("1500")
    assert rows[0]["derived_value"] == Decimal("2000")


def test_v_data_freshness_reports_per_source(loaded_conn):
    rows = {
        r["source_table"]: r
        for r in _rows(loaded_conn, "SELECT * FROM marts.v_data_freshness")
    }
    assert rows["transactions"]["max_date"] == date(2026, 2, 15)
    assert rows["transactions"]["row_count"] == 5
    assert rows["transactions"]["last_successful_sync"] is not None
    assert rows["prices"]["max_date"] == date(2026, 2, 20)
    assert rows["prices"]["row_count"] == 1


def test_v_transactions_joins_account_and_instrument(loaded_conn):
    rows = _rows(
        loaded_conn,
        "SELECT * FROM marts.v_transactions WHERE txn_type = 'BUY'",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["broker"] == "Demo Broker"
    assert row["instrument_name"] == "Demo Corp"
    assert row["asset_class"] == "EQUITY"


def test_v_lookthrough_allocation_matches_v_allocation_for_complete_coverage(
    loaded_conn,
):
    with loaded_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, quantity, price, gross_amount, net_amount, currency)
            VALUES ('csv:t:etf', 'hashetf', 'DEMO-BROKER-CTO', '2026-03-01', 'BUY',
                    'DEMO-ETF-WORLD', 10, 100, 1000, -1000, 'EUR')
            """
        )
        cur.execute(
            "INSERT INTO core.prices (instrument_id, price_date, close_price, "
            "currency, source) VALUES ('DEMO-ETF-WORLD', '2026-03-01', 100, 'EUR', 'yfinance')"
        )
        cur.execute(
            """
            INSERT INTO core.etp_exposure
                (instrument_id, as_of_date, dimension, code, label, weight, source)
            VALUES
                ('DEMO-ETF-WORLD', '2026-03-01', 'sector', 'TECHNOLOGY',
                 'Technology', 0.6, 'justetf'),
                ('DEMO-ETF-WORLD', '2026-03-01', 'sector', 'FINANCE',
                 'Finance', 0.4, 'justetf'),
                ('DEMO-ETF-WORLD', '2026-03-01', 'country', 'US',
                 'United States', 1.0, 'justetf')
            """
        )
    loaded_conn.commit()

    # DEMO-SHARE (600, sector=TECHNOLOGY, domicile=FR) is a direct holding
    # at weight 1; DEMO-ETF-WORLD (1000) decomposes into TECHNOLOGY 600 +
    # FINANCE 400, and country US 1000. Both dimensions have complete
    # coverage (weights sum to 1, and the direct holding is weight 1) —
    # totals must match v_allocation's non-cash total exactly.
    total_alloc = _rows(
        loaded_conn,
        "SELECT sum(value_base) AS t FROM marts.v_allocation WHERE asset_class <> 'CASH'",
    )[0]["t"]

    sector_total = _rows(
        loaded_conn,
        "SELECT sum(value_base) AS t FROM marts.v_lookthrough_allocation "
        "WHERE dimension = 'sector'",
    )[0]["t"]
    country_total = _rows(
        loaded_conn,
        "SELECT sum(value_base) AS t FROM marts.v_lookthrough_allocation "
        "WHERE dimension = 'country'",
    )[0]["t"]

    assert total_alloc == Decimal("1600.00000000000000000000000000")
    assert sector_total == total_alloc
    assert country_total == total_alloc

    # Same code from two different raw labels ("TECHNOLOGY" the stored
    # instrument sector vs "Technology" the scraped exposure label) must
    # collapse to one row, not split the pie.
    technology_rows = _rows(
        loaded_conn,
        "SELECT * FROM marts.v_lookthrough_allocation "
        "WHERE dimension = 'sector' AND code = 'TECHNOLOGY'",
    )
    assert len(technology_rows) == 1
    assert technology_rows[0]["value_base"] == Decimal(
        "1200.00000000000000000000000000000000"
    )
    assert technology_rows[0]["label"] == "Technology"


def test_v_positions_excludes_fully_sold_position(seeded_conn):
    with seeded_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, quantity, price, gross_amount, net_amount, currency)
            VALUES
                ('csv:t:1', 'h1', 'DEMO-BROKER-CTO', '2026-01-05', 'BUY',
                 'DEMO-SHARE', 10, 100, 1000, -1000, 'EUR'),
                ('csv:t:2', 'h2', 'DEMO-BROKER-CTO', '2026-02-01', 'SELL',
                 'DEMO-SHARE', -10, 110, 1100, 1100, 'EUR')
            """
        )
    seeded_conn.commit()
    assert _rows(seeded_conn, "SELECT * FROM marts.v_positions") == []
