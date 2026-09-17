from datetime import date
from decimal import Decimal

import pytest

from folios.seed import seed
from tests.test_seed import EXAMPLE_CONFIG


@pytest.fixture()
def seeded_conn(migrated_conn):
    seed(migrated_conn, EXAMPLE_CONFIG)
    return migrated_conn


def _insert_txn(conn, entry_id, account, trade_date, txn_type, instrument, qty, price, gross,
                 net, currency="EUR", fee=Decimal("0"), fx_rate=Decimal("1")):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, quantity, price, gross_amount, fee, net_amount,
                 currency, fx_rate_to_base)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (entry_id, f"hash-{entry_id}", account, trade_date, txn_type, instrument,
             qty, price, gross, fee, net, currency, fx_rate),
        )
    conn.commit()


def _walk_rows(conn, instrument_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date, txn_type, qty, cost_local, avg_cost_local, "
            "realised_local, currency_effect FROM marts.v_ledger_walk "
            "WHERE instrument_id = %s ORDER BY n",
            (instrument_id,),
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def test_worked_example_from_build_plan(seeded_conn):
    # The exact example from build-plan step 11's own "Done when".
    _insert_txn(seeded_conn, "t1", "DEMO-BROKER-CTO", date(2026, 1, 12), "BUY",
                "DEMO-SHARE", 10, 100, 1000, -1002, fee=Decimal("2"))
    _insert_txn(seeded_conn, "t2", "DEMO-BROKER-CTO", date(2026, 3, 3), "BUY",
                "DEMO-SHARE", 10, 140, 1400, -1402, fee=Decimal("2"))
    _insert_txn(seeded_conn, "t3", "DEMO-BROKER-CTO", date(2026, 6, 5), "SELL",
                "DEMO-SHARE", -8, 160, 1280, 1278, fee=Decimal("2"))

    rows = _walk_rows(seeded_conn, "DEMO-SHARE")
    assert len(rows) == 3

    buy1, buy2, sell = rows
    assert buy1["qty"] == Decimal("10")
    assert buy1["cost_local"] == Decimal("1002.0000")
    assert buy1["avg_cost_local"] == Decimal("100.20")

    assert buy2["qty"] == Decimal("20")
    assert buy2["cost_local"] == Decimal("2404.0000")
    assert buy2["avg_cost_local"] == Decimal("120.20")

    assert sell["qty"] == Decimal("12")
    assert sell["cost_local"] == Decimal("1442.40")
    assert sell["avg_cost_local"] == Decimal("120.20")  # unchanged by the sale
    assert sell["realised_local"] == Decimal("316.40")
    assert sell["currency_effect"] == Decimal("0")  # EUR instrument, fx always 1


def test_selling_the_whole_position_leaves_cost_at_exactly_zero(seeded_conn):
    _insert_txn(seeded_conn, "t1", "DEMO-BROKER-CTO", date(2026, 1, 1), "BUY",
                "DEMO-CRYPTO-BTC", 10, 100, 1000, -1000)
    _insert_txn(seeded_conn, "t2", "DEMO-BROKER-CTO", date(2026, 2, 1), "SELL",
                "DEMO-CRYPTO-BTC", -10, 110, 1100, 1100)

    rows = _walk_rows(seeded_conn, "DEMO-CRYPTO-BTC")
    final = rows[-1]
    assert final["qty"] == Decimal("0")
    assert final["cost_local"] == Decimal("0")  # exactly zero, to the cent
    assert final["avg_cost_local"] is None  # guarded, not a division error


def test_four_for_one_split_quadruples_quantity_and_quarters_average_cost(seeded_conn):
    _insert_txn(seeded_conn, "t1", "DEMO-BROKER-CTO", date(2026, 1, 1), "BUY",
                "DEMO-ETF-WORLD", 100, 100, 10000, -10000)
    # CSV/loader convention: SPLIT's quantity is the delta, not the new
    # total — a 4:1 split on 100 shares adds 300.
    _insert_txn(seeded_conn, "t2", "DEMO-BROKER-CTO", date(2026, 2, 1), "SPLIT",
                "DEMO-ETF-WORLD", 300, None, None, 0)

    rows = _walk_rows(seeded_conn, "DEMO-ETF-WORLD")
    split = rows[-1]
    assert split["qty"] == Decimal("400")
    assert split["cost_local"] == Decimal("10000")  # unchanged by a split
    assert split["avg_cost_local"] == Decimal("25")


def test_realised_and_avg_cost_use_the_pre_sale_average(seeded_conn):
    # A second sale at a wildly different price must not move the average
    # computed from the first sale — proving there are no lots being
    # tracked, just a single running average.
    _insert_txn(seeded_conn, "t1", "DEMO-BROKER-CTO", date(2026, 1, 1), "BUY",
                "DEMO-SHARE", 10, 100, 1000, -1000)
    _insert_txn(seeded_conn, "t2", "DEMO-BROKER-CTO", date(2026, 2, 1), "SELL",
                "DEMO-SHARE", -2, 500, 1000, 1000)
    _insert_txn(seeded_conn, "t3", "DEMO-BROKER-CTO", date(2026, 3, 1), "SELL",
                "DEMO-SHARE", -2, 5, 10, 10)

    rows = _walk_rows(seeded_conn, "DEMO-SHARE")
    assert rows[1]["avg_cost_local"] == Decimal("100")
    assert rows[1]["realised_local"] == Decimal("800")  # 1000 - 100*2
    assert rows[2]["avg_cost_local"] == Decimal("100")  # still 100, unmoved
    assert rows[2]["realised_local"] == Decimal("-190")  # 10 - 100*2


def test_currency_effect_is_zero_for_a_eur_instrument(seeded_conn):
    _insert_txn(seeded_conn, "t1", "DEMO-BROKER-CTO", date(2026, 1, 1), "BUY",
                "DEMO-SHARE", 10, 100, 1000, -1000)
    _insert_txn(seeded_conn, "t2", "DEMO-BROKER-CTO", date(2026, 2, 1), "SELL",
                "DEMO-SHARE", -10, 110, 1100, 1100)
    rows = _walk_rows(seeded_conn, "DEMO-SHARE")
    assert rows[-1]["currency_effect"] == Decimal("0")


def test_currency_effect_reflects_fx_movement_for_a_foreign_instrument(seeded_conn):
    with seeded_conn.cursor() as cur:
        cur.execute(
            "UPDATE core.instruments SET currency = 'USD' WHERE instrument_id = 'DEMO-BOND-2030'"
        )
    seeded_conn.commit()

    _insert_txn(seeded_conn, "t1", "DEMO-BROKER-CTO", date(2026, 1, 1), "BUY",
                "DEMO-BOND-2030", 10, 100, 1000, -1000, currency="USD", fx_rate=Decimal("1.10"))
    _insert_txn(seeded_conn, "t2", "DEMO-BROKER-CTO", date(2026, 6, 1), "SELL",
                "DEMO-BOND-2030", -10, 110, 1100, 1100, currency="USD", fx_rate=Decimal("1.20"))

    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT realised_local, realised_base, currency_effect FROM marts.v_ledger_walk "
            "WHERE instrument_id = 'DEMO-BOND-2030' ORDER BY n"
        )
        rows = cur.fetchall()
    realised_local, realised_base, currency_effect = rows[-1]
    assert realised_local == Decimal("100")  # 1100 - 100*10, in USD
    assert realised_base == Decimal("220")  # 1320 - 110*10, in EUR
    assert currency_effect == realised_base - realised_local == Decimal("120")


def test_ledger_walk_is_partitioned_per_account_and_instrument(seeded_conn):
    _insert_txn(seeded_conn, "t1", "DEMO-BROKER-CTO", date(2026, 1, 1), "BUY",
                "DEMO-SHARE", 10, 100, 1000, -1000)
    _insert_txn(seeded_conn, "t2", "DEMO-BANK-CURRENT", date(2026, 1, 1), "OPENING_BALANCE",
                "DEMO-SHARE", 5, 90, 450, -450)

    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT account_id, qty, cost_local FROM marts.v_ledger_walk "
            "WHERE instrument_id = 'DEMO-SHARE' ORDER BY account_id"
        )
        rows = {r[0]: r[1:] for r in cur.fetchall()}
    assert rows["DEMO-BROKER-CTO"] == (Decimal("10"), Decimal("1000"))
    assert rows["DEMO-BANK-CURRENT"] == (Decimal("5"), Decimal("450"))
