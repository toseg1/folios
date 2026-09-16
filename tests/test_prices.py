from datetime import date, timedelta
from decimal import Decimal

import pytest

from folios.prices import (
    determine_tickers_to_fetch,
    instruments_ever_held,
    refresh_prices,
    store_prices,
)
from folios.seed import seed
from tests.test_seed import EXAMPLE_CONFIG


class FakePriceProvider:
    """Deterministic stand-in for YFinancePriceProvider — no live network."""

    def __init__(self, prices):
        self._prices = prices
        self.calls: list[tuple[dict, date]] = []

    def fetch_prices(self, tickers, since):
        self.calls.append((dict(tickers), since))
        return {k: v for k, v in self._prices.items() if k in tickers}


@pytest.fixture()
def seeded_conn(migrated_conn):
    seed(migrated_conn, EXAMPLE_CONFIG)
    return migrated_conn


def _buy(conn, account, instrument_id, trade_date, entry_suffix):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, quantity, price, net_amount, currency)
            VALUES (%s, %s, %s, %s, 'BUY', %s, 1, 100, -100, 'EUR')
            """,
            (f"csv:test:{entry_suffix}", f"hash{entry_suffix}", account,
             trade_date, instrument_id),
        )
    conn.commit()


def test_instruments_ever_held_only_quantity_affecting_types(seeded_conn):
    _buy(seeded_conn, "DEMO-BROKER-CTO", "DEMO-SHARE", date(2026, 1, 5), 1)
    held = {row["instrument_id"] for row in instruments_ever_held(seeded_conn)}
    assert held == {"DEMO-SHARE"}


def test_determine_tickers_skips_manual_price_source(seeded_conn):
    # DEMO-SHARE is yfinance-priced, DEMO-FUND-BOND is manual, per
    # config/example/instruments.csv.
    _buy(seeded_conn, "DEMO-BROKER-CTO", "DEMO-SHARE", date(2026, 1, 5), 1)
    _buy(seeded_conn, "DEMO-BROKER-CTO", "DEMO-FUND-BOND", date(2026, 1, 5), 2)

    tickers, earliest, warnings = determine_tickers_to_fetch(seeded_conn)
    assert tickers == {"DEMO-SHARE": "DEMO.PA"}
    assert earliest == date(2026, 1, 5)
    assert warnings == []  # manual instruments are never reported


def test_determine_tickers_warns_on_missing_yf_symbol(seeded_conn):
    with seeded_conn.cursor() as cur:
        cur.execute(
            "UPDATE core.instruments SET yf_symbol = NULL WHERE instrument_id = 'DEMO-SHARE'"
        )
    seeded_conn.commit()
    _buy(seeded_conn, "DEMO-BROKER-CTO", "DEMO-SHARE", date(2026, 1, 5), 1)

    tickers, earliest, warnings = determine_tickers_to_fetch(seeded_conn)
    assert tickers == {}
    assert earliest is None
    assert len(warnings) == 1
    assert "DEMO-SHARE" in warnings[0]
    assert "no yf_symbol" in warnings[0]


def test_determine_tickers_gap_start_resumes_after_last_stored_price(seeded_conn):
    _buy(seeded_conn, "DEMO-BROKER-CTO", "DEMO-SHARE", date(2026, 1, 5), 1)
    store_prices(
        seeded_conn, {"DEMO-SHARE": {date(2026, 3, 1): (Decimal("10"), Decimal("10"))}}
    )
    _tickers, earliest, _warnings = determine_tickers_to_fetch(seeded_conn)
    assert earliest == date(2026, 3, 2)


def test_store_prices_is_idempotent(seeded_conn):
    prices = {"DEMO-SHARE": {date(2026, 3, 1): (Decimal("10.50"), Decimal("10.50"))}}
    assert store_prices(seeded_conn, prices) == 1
    assert store_prices(seeded_conn, prices) == 1  # upsert, not duplicate

    with seeded_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.prices")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT currency FROM core.prices WHERE instrument_id = 'DEMO-SHARE'")
        assert cur.fetchone()[0] == "EUR"  # pulled from core.instruments


def test_refresh_prices_stores_and_reports_warnings(seeded_conn):
    _buy(seeded_conn, "DEMO-BROKER-CTO", "DEMO-SHARE", date(2026, 1, 5), 1)
    fake = FakePriceProvider(
        {"DEMO-SHARE": {date(2026, 1, 6): (Decimal("101"), Decimal("101"))}}
    )
    stored, warnings = refresh_prices(seeded_conn, provider=fake)
    assert stored == 1
    assert warnings == []
    assert fake.calls[0][0] == {"DEMO-SHARE": "DEMO.PA"}


def test_refresh_prices_second_run_inserts_nothing_new(seeded_conn):
    _buy(seeded_conn, "DEMO-BROKER-CTO", "DEMO-SHARE", date(2026, 1, 5), 1)
    fake = FakePriceProvider(
        {"DEMO-SHARE": {date(2026, 1, 5): (Decimal("100"), Decimal("100"))}}
    )
    refresh_prices(seeded_conn, provider=fake)

    with seeded_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.prices")
        before = cur.fetchone()[0]

    # A provider whose data doesn't extend past what's already stored —
    # the gap has closed, nothing new to add.
    empty_provider = FakePriceProvider({})
    stored, _warnings = refresh_prices(seeded_conn, provider=empty_provider)
    assert stored == 0

    with seeded_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.prices")
        after = cur.fetchone()[0]
    assert before == after == 1


def test_refresh_prices_nothing_ever_held_is_a_noop(seeded_conn):
    stored, warnings = refresh_prices(seeded_conn, provider=FakePriceProvider({}))
    assert stored == 0
    assert warnings == []


def test_fetch_prices_skips_future_since_date():
    from folios.prices import YFinancePriceProvider

    provider = YFinancePriceProvider()
    result = provider.fetch_prices(
        {"X": "AAPL"}, date.today() + timedelta(days=1)
    )
    assert result == {}
