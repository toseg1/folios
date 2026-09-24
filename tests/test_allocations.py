from datetime import date
from decimal import Decimal

import pytest

from folios.allocations import (
    MissingPriceError,
    NoAllocationError,
    current_allocation,
    expand_contribution,
    resolve_price_with_date,
    split_amount,
)
from folios.seed import seed
from tests.test_seed import EXAMPLE_CONFIG


@pytest.fixture()
def seeded_conn(migrated_conn):
    seed(migrated_conn, EXAMPLE_CONFIG)
    return migrated_conn


def _store_price(conn, instrument_id: str, price_date: date, close_price: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.prices (instrument_id, price_date, close_price, currency) "
            "VALUES (%s, %s, %s, 'EUR')",
            (instrument_id, price_date, Decimal(close_price)),
        )
    conn.commit()


def test_split_amount_sums_exactly_to_the_total():
    weights = [
        ("A", Decimal("1") / Decimal("3")),
        ("B", Decimal("1") / Decimal("3")),
        ("C", Decimal("1") / Decimal("3")),
    ]
    splits = split_amount(Decimal("1000.00"), weights)
    assert sum(amount for _, amount in splits) == Decimal("1000.00")
    # largest-remainder: the extra cent goes to the earliest instrument_id
    # among the tied largest remainders.
    assert dict(splits) == {
        "A": Decimal("333.34"), "B": Decimal("333.33"), "C": Decimal("333.33"),
    }


def test_split_amount_uneven_weights():
    weights = [("A", Decimal("0.6")), ("B", Decimal("0.4"))]
    splits = split_amount(Decimal("100.01"), weights)
    assert sum(amount for _, amount in splits) == Decimal("100.01")


def test_current_allocation_picks_latest_block_on_or_before(seeded_conn):
    weights = current_allocation(seeded_conn, "LINXEA-SPIRIT-AV", date(2026, 1, 1))
    assert dict(weights) == {
        "DEMO-ETF-WORLD": Decimal("0.6"), "DEMO-FONDS-EUROS": Decimal("0.4"),
    }


def test_current_allocation_prefers_a_later_arbitrage_block(seeded_conn):
    with seeded_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.account_target_allocations "
            "(account_id, as_of_date, instrument_id, weight) VALUES "
            "('LINXEA-SPIRIT-AV', '2026-06-01', 'DEMO-ETF-WORLD', 0.8), "
            "('LINXEA-SPIRIT-AV', '2026-06-01', 'DEMO-FONDS-EUROS', 0.2)"
        )
    seeded_conn.commit()

    before = current_allocation(seeded_conn, "LINXEA-SPIRIT-AV", date(2026, 5, 1))
    after = current_allocation(seeded_conn, "LINXEA-SPIRIT-AV", date(2026, 6, 15))

    assert dict(before)["DEMO-ETF-WORLD"] == Decimal("0.6")
    assert dict(after)["DEMO-ETF-WORLD"] == Decimal("0.8")


def test_current_allocation_raises_with_no_snapshot(seeded_conn):
    with pytest.raises(NoAllocationError):
        current_allocation(seeded_conn, "BOURSOBANK-CTO", date(2026, 1, 1))


def test_resolve_price_with_date_latest_on_or_before(seeded_conn):
    _store_price(seeded_conn, "DEMO-ETF-WORLD", date(2026, 1, 1), "100")
    _store_price(seeded_conn, "DEMO-ETF-WORLD", date(2026, 2, 1), "110")

    price, price_date = resolve_price_with_date(
        seeded_conn, "DEMO-ETF-WORLD", date(2026, 1, 15)
    )
    assert price == Decimal("100")
    assert price_date == date(2026, 1, 1)


def test_resolve_price_with_date_none_when_unpriced(seeded_conn):
    assert resolve_price_with_date(seeded_conn, "DEMO-ETF-WORLD", date(2026, 1, 1)) is None


def test_expand_contribution_computes_quantity_per_instrument(seeded_conn):
    _store_price(seeded_conn, "DEMO-ETF-WORLD", date(2026, 1, 1), "100")
    _store_price(seeded_conn, "DEMO-FONDS-EUROS", date(2026, 1, 1), "1")

    rows = expand_contribution(
        seeded_conn, "LINXEA-SPIRIT-AV", date(2026, 1, 10), Decimal("1000")
    )
    by_instrument = {r["instrument_id"]: r for r in rows}

    assert sum(r["gross_amount"] for r in rows) == Decimal("1000")
    assert by_instrument["DEMO-ETF-WORLD"]["quantity"] == Decimal("6")
    assert by_instrument["DEMO-FONDS-EUROS"]["quantity"] == Decimal("400")
    assert by_instrument["DEMO-ETF-WORLD"]["net_amount"] == Decimal("-600")


def test_expand_contribution_raises_when_a_fund_has_no_price(seeded_conn):
    _store_price(seeded_conn, "DEMO-ETF-WORLD", date(2026, 1, 1), "100")
    # DEMO-FONDS-EUROS deliberately left unpriced.

    with pytest.raises(MissingPriceError, match="DEMO-FONDS-EUROS"):
        expand_contribution(
            seeded_conn, "LINXEA-SPIRIT-AV", date(2026, 1, 10), Decimal("1000")
        )
