from datetime import date, timedelta
from decimal import Decimal

import pytest

from folios.seed import seed
from tests.test_seed import EXAMPLE_CONFIG


@pytest.fixture()
def seeded_conn(migrated_conn):
    seed(migrated_conn, EXAMPLE_CONFIG)
    return migrated_conn


def _rows(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


@pytest.fixture()
def worked_example_conn(seeded_conn):
    """Hand-computed worked example, used across every view in this file:
    BUY 10@100 fee2 (net -1002) -> SELL 8@160 fee2 (net 1278) on
    DEMO-SHARE, a DIVIDEND (gross 9.60, tax 1.44, net 8.16), a standalone
    FEE of 5, and a current price of 150.

    By hand:
      avg_cost after BUY = 100.20
      SELL: proceeds 1278, cost removed 100.20*8=801.60, realised 476.40
      Remaining position: qty=2, cost=200.40, avg=100.20 (unchanged)
      unrealised = 2*150 - 200.40 = 99.60
      income = 8.16
      costs = 2 (buy fee) + 2 (sell fee) + 5 (standalone FEE) = 9.00
      total_return = 476.40 + 99.60 + 8.16 - 9.00 = 575.16
    """
    with seeded_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, quantity, price, gross_amount, fee, net_amount, currency)
            VALUES
                ('csv:t:1', 'h1', 'DEMO-BROKER-CTO', '2026-01-12', 'BUY', 'DEMO-SHARE',
                 10, 100, 1000, 2, -1002, 'EUR'),
                ('csv:t:2', 'h2', 'DEMO-BROKER-CTO', '2026-06-05', 'SELL', 'DEMO-SHARE',
                 -8, 160, 1280, 2, 1278, 'EUR')
            """
        )
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, gross_amount, tax, net_amount, currency)
            VALUES ('csv:t:3', 'h3', 'DEMO-BROKER-CTO', '2026-02-01', 'DIVIDEND',
                    'DEMO-SHARE', 9.60, 1.44, 8.16, 'EUR')
            """
        )
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 gross_amount, net_amount, currency)
            VALUES ('csv:t:4', 'h4', 'DEMO-BROKER-CTO', '2026-02-10', 'FEE', 5, -5, 'EUR')
            """
        )
        cur.execute(
            "INSERT INTO core.prices (instrument_id, price_date, close_price, "
            "currency, source) VALUES ('DEMO-SHARE', '2026-07-01', 150, 'EUR', 'yfinance')"
        )
    seeded_conn.commit()
    return seeded_conn


def test_v_realised_pnl_matches_hand_computed_result(worked_example_conn):
    rows = _rows(worked_example_conn, "SELECT * FROM marts.v_realised_pnl")
    assert len(rows) == 1
    row = rows[0]
    assert row["quantity_sold"] == Decimal("-8")
    assert row["proceeds_local"] == Decimal("1278.0000")
    assert row["cost_of_disposal_local"] == Decimal("801.60")
    assert row["realised_local"] == Decimal("476.40")
    assert row["currency_effect"] == Decimal("0")


def test_v_realised_pnl_excludes_transfer_out(seeded_conn):
    with seeded_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, quantity, price, net_amount, currency)
            VALUES
                ('csv:t:1', 'h1', 'DEMO-BROKER-CTO', '2026-01-01', 'BUY',
                 'DEMO-SHARE', 10, 100, -1000, 'EUR'),
                ('csv:t:2', 'h2', 'DEMO-BROKER-CTO', '2026-02-01', 'TRANSFER_OUT',
                 'DEMO-SHARE', -10, NULL, 0, 'EUR')
            """
        )
    seeded_conn.commit()
    # A custody transfer is not a disposal — it must not show up as a
    # "loss" in a P&L-labelled view, even though v_ledger_walk itself
    # computes a mechanical "realised" for it.
    assert _rows(seeded_conn, "SELECT * FROM marts.v_realised_pnl") == []


def test_v_income_three_streams(seeded_conn):
    with seeded_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, gross_amount, tax, net_amount, currency)
            VALUES
                ('csv:t:1', 'h1', 'DEMO-BROKER-CTO', '2026-01-01', 'DIVIDEND',
                 'DEMO-SHARE', 100, 15, 85, 'EUR'),
                ('csv:t:2', 'h2', 'DEMO-BANK-CURRENT', '2026-01-01', 'INTEREST',
                 NULL, 20, 0, 20, 'EUR'),
                ('csv:t:3', 'h3', 'DEMO-BROKER-CTO', '2026-01-01', 'STAKING',
                 'DEMO-CRYPTO-BTC', 50, 0, 0, 'EUR')
            """
        )
    seeded_conn.commit()
    rows = {r["stream"]: r for r in _rows(seeded_conn, "SELECT * FROM marts.v_income")}
    assert rows["DIVIDEND"]["net"] == Decimal("85")
    assert rows["INTEREST"]["instrument_id"] is None  # a Livret A / current account
    assert rows["STAKING"]["gross"] == Decimal("50")
    assert rows["STAKING"]["net"] == Decimal("50")  # in kind, no withholding


def test_v_fee_drag_uses_ongoing_charges_times_market_value(worked_example_conn):
    with worked_example_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, quantity, price, gross_amount, net_amount, currency)
            VALUES ('csv:t:5', 'h5', 'DEMO-BROKER-CTO', '2026-03-01', 'BUY',
                    'DEMO-ETF-WORLD', 10, 100, 1000, -1000, 'EUR')
            """
        )
        cur.execute(
            "INSERT INTO core.prices (instrument_id, price_date, close_price, "
            "currency, source) VALUES ('DEMO-ETF-WORLD', '2026-03-01', 100, 'EUR', 'yfinance')"
        )
    worked_example_conn.commit()

    rows = _rows(worked_example_conn, "SELECT * FROM marts.v_fee_drag")
    assert len(rows) == 1
    assert rows[0]["ongoing_charges"] == Decimal("0.002000")
    assert rows[0]["annual_fee_drag_base"] == Decimal("2.00000000000000000000000000000000")


def test_v_custody_exposure_three_layers(worked_example_conn):
    with worked_example_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, quantity, price, gross_amount, net_amount, currency)
            VALUES ('csv:t:5', 'h5', 'DEMO-BROKER-CTO', '2026-03-01', 'BUY',
                    'DEMO-ETF-WORLD', 10, 100, 1000, -1000, 'EUR')
            """
        )
        cur.execute(
            "INSERT INTO core.prices (instrument_id, price_date, close_price, "
            "currency, source) VALUES ('DEMO-ETF-WORLD', '2026-03-01', 100, 'EUR', 'yfinance')"
        )
    worked_example_conn.commit()

    rows = {
        r["layer"]: r
        for r in _rows(worked_example_conn, "SELECT * FROM marts.v_custody_exposure")
    }
    assert rows["ultimate_parent"]["custodian"] == "Demo Broker SA"
    assert rows["ultimate_parent"]["value_base"] == Decimal("1300.00000000000000000000000000")
    assert rows["fund_custodian"]["custodian"] == "Demo Depositary Bank"
    assert rows["fund_custodian"]["value_base"] == Decimal("1000.00000000000000000000000000")


def test_v_risk_profile_is_a_distribution_not_an_average(worked_example_conn):
    with worked_example_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, quantity, price, gross_amount, net_amount, currency)
            VALUES ('csv:t:5', 'h5', 'DEMO-BROKER-CTO', '2026-03-01', 'BUY',
                    'DEMO-ETF-WORLD', 10, 100, 1000, -1000, 'EUR')
            """
        )
        cur.execute(
            "INSERT INTO core.prices (instrument_id, price_date, close_price, "
            "currency, source) VALUES ('DEMO-ETF-WORLD', '2026-03-01', 100, 'EUR', 'yfinance')"
        )
    worked_example_conn.commit()

    rows = _rows(worked_example_conn, "SELECT * FROM marts.v_risk_profile")
    assert rows == [{"sri": 4, "value_base": Decimal("1000.00000000000000000000000000")}]


def test_v_wrapper_risk_direct_holding_fallback(worked_example_conn):
    rows = {
        r["wrapper_risk"]: r["value_base"]
        for r in _rows(worked_example_conn, "SELECT * FROM marts.v_wrapper_risk")
    }
    assert rows["DIRECT_HOLDING"] == Decimal("300.00000000000000000000000000")


def test_v_wrapper_risk_ucits_fund(worked_example_conn):
    with worked_example_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, quantity, price, gross_amount, net_amount, currency)
            VALUES ('csv:t:5', 'h5', 'DEMO-BROKER-CTO', '2026-03-01', 'BUY',
                    'DEMO-ETF-WORLD', 10, 100, 1000, -1000, 'EUR')
            """
        )
        cur.execute(
            "INSERT INTO core.prices (instrument_id, price_date, close_price, "
            "currency, source) VALUES ('DEMO-ETF-WORLD', '2026-03-01', 100, 'EUR', 'yfinance')"
        )
    worked_example_conn.commit()

    rows = {
        r["wrapper_risk"]: r["value_base"]
        for r in _rows(worked_example_conn, "SELECT * FROM marts.v_wrapper_risk")
    }
    assert rows["UCITS_FUND"] == Decimal("1000.00000000000000000000000000")
    assert rows["DIRECT_HOLDING"] == Decimal("300.00000000000000000000000000")


def test_v_lookthrough_coverage_sums_weight_and_counts_constituents(seeded_conn):
    with seeded_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.etp_exposure
                (instrument_id, as_of_date, dimension, code, label, weight, source)
            VALUES
                ('DEMO-ETF-WORLD', '2026-03-01', 'sector', 'TECHNOLOGY',
                 'Technology', 0.6, 'justetf'),
                ('DEMO-ETF-WORLD', '2026-03-01', 'sector', 'FINANCE',
                 'Finance', 0.4, 'justetf')
            """
        )
    seeded_conn.commit()
    rows = _rows(seeded_conn, "SELECT * FROM marts.v_lookthrough_coverage")
    assert len(rows) == 1
    assert rows[0]["coverage"] == Decimal("1.000000")
    assert rows[0]["constituent_count"] == 2


def test_v_income_yield_is_income_over_average_cost_basis(worked_example_conn):
    rows = _rows(worked_example_conn, "SELECT * FROM marts.v_income_yield")
    assert len(rows) == 1
    row = rows[0]
    assert row["instrument_id"] == "DEMO-SHARE"
    assert row["income_base"] == Decimal("8.16000000000000")
    assert row["total_cost_basis_base"] == Decimal(
        "200.400000000000000000000000000000000000000000000000000000"
    )
    # Postgres numeric division and Python's decimal module round to
    # different precisions — compare via the inverse multiplication
    # instead of re-dividing and expecting bit-identical results.
    assert abs(
        row["yield_on_cost"] * row["total_cost_basis_base"] - row["income_base"]
    ) < Decimal("0.0001")


def test_v_income_yield_excludes_income_older_than_12_months(seeded_conn):
    old_date = date.today() - timedelta(days=400)
    with seeded_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, quantity, price, gross_amount, net_amount, currency)
            VALUES ('csv:t:1', 'h1', 'DEMO-BROKER-CTO', %s, 'BUY',
                    'DEMO-SHARE', 10, 100, 1000, -1000, 'EUR')
            """,
            (old_date,),
        )
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, gross_amount, tax, net_amount, currency)
            VALUES ('csv:t:2', 'h2', 'DEMO-BROKER-CTO', %s, 'DIVIDEND',
                    'DEMO-SHARE', 100, 0, 100, 'EUR')
            """,
            (old_date,),
        )
    seeded_conn.commit()
    assert _rows(seeded_conn, "SELECT * FROM marts.v_income_yield") == []


def test_v_pnl_summary_matches_hand_computed_total(worked_example_conn):
    rows = _rows(worked_example_conn, "SELECT * FROM marts.v_pnl_summary")
    assert len(rows) == 1
    row = rows[0]
    assert row["realised_base"] == Decimal("476.40")
    assert row["unrealised_base"] == Decimal("99.60")
    assert row["income_base"] == Decimal("8.16000000000000")
    assert row["costs_base"] == Decimal("9.00000000000000")
    assert row["realised_currency_effect"] == Decimal("0")
    assert row["total_return"] == Decimal("575.16")


def test_v_pnl_summary_total_equals_sum_of_its_own_components(worked_example_conn):
    row = _rows(worked_example_conn, "SELECT * FROM marts.v_pnl_summary")[0]
    assert row["total_return"] == (
        row["realised_base"] + row["unrealised_base"] + row["income_base"] - row["costs_base"]
    )


def test_removing_income_changes_total_by_exactly_that_component(worked_example_conn):
    # A dividend touches income only — never quantity, cost basis, or
    # realised (per this step's own pinned-down convention) — so it's the
    # one component that can be added/removed in isolation to prove the
    # build plan's "Done when": total changes by exactly that component's
    # value, nothing else shifts.
    before = _rows(worked_example_conn, "SELECT * FROM marts.v_pnl_summary")[0]

    with worked_example_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, gross_amount, tax, net_amount, currency)
            VALUES ('csv:t:extra-div', 'hx', 'DEMO-BROKER-CTO', '2026-02-15',
                    'DIVIDEND', 'DEMO-SHARE', 50, 0, 50, 'EUR')
            """
        )
    worked_example_conn.commit()
    after = _rows(worked_example_conn, "SELECT * FROM marts.v_pnl_summary")[0]

    assert after["income_base"] - before["income_base"] == Decimal("50")
    assert after["realised_base"] == before["realised_base"]
    assert after["unrealised_base"] == before["unrealised_base"]
    assert after["costs_base"] == before["costs_base"]
    assert after["total_return"] - before["total_return"] == Decimal("50")
