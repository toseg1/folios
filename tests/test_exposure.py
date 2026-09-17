from datetime import date
from decimal import Decimal

import pytest

from folios.exposure import (
    etps_held,
    load_exposure_mapping,
    refresh_exposure,
    store_exposure,
)
from folios.seed import seed
from tests.test_seed import EXAMPLE_CONFIG

EXPOSURE_MAPPING = EXAMPLE_CONFIG / "exposure_mapping.csv"


class FakeExposureProvider:
    def __init__(self, by_isin=None, raise_for=()):
        self._by_isin = by_isin or {}
        self._raise_for = set(raise_for)
        self.calls: list[str] = []

    def fetch_exposure(self, isin: str):
        self.calls.append(isin)
        if isin in self._raise_for:
            raise RuntimeError("justETF page changed, could not parse")
        return self._by_isin[isin]


DEMO_WORLD_EXPOSURE = {
    "sector": [
        ("Technology", Decimal("0.3488")),
        ("Finance", Decimal("0.1847")),
        ("Industrials", Decimal("0.0972")),
        ("Healthcare", Decimal("0.0900")),
        ("Other", Decimal("0.2793")),
    ],
    "country": [
        ("United States", Decimal("0.6907")),
        ("Japan", Decimal("0.0567")),
        ("United Kingdom", Decimal("0.0376")),
        ("Canada", Decimal("0.0342")),
        ("Other", Decimal("0.1808")),
    ],
}


@pytest.fixture()
def seeded_conn(migrated_conn):
    seed(migrated_conn, EXAMPLE_CONFIG)
    return migrated_conn


def _buy(conn, instrument_id, entry_suffix, quantity=1):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, quantity, price, net_amount, currency)
            VALUES (%s, %s, 'DEMO-BROKER-CTO', '2026-01-05', 'BUY',
                    %s, %s, 100, -100, 'EUR')
            """,
            (f"csv:test:{entry_suffix}", f"hash{entry_suffix}", instrument_id, quantity),
        )
    conn.commit()


def test_etps_held_only_currently_held_etps(seeded_conn):
    _buy(seeded_conn, "DEMO-ETF-WORLD", 1)
    _buy(seeded_conn, "DEMO-SHARE", 2)  # not an ETP, must not appear
    held = {row["instrument_id"] for row in etps_held(seeded_conn)}
    assert held == {"DEMO-ETF-WORLD"}


def test_etps_held_excludes_fully_sold_position(seeded_conn):
    _buy(seeded_conn, "DEMO-ETF-WORLD", 1, quantity=1)
    with seeded_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, quantity, price, net_amount, currency)
            VALUES ('csv:test:2', 'hash2', 'DEMO-BROKER-CTO', '2026-02-01', 'SELL',
                    'DEMO-ETF-WORLD', -1, 100, 100, 'EUR')
            """
        )
    seeded_conn.commit()
    assert etps_held(seeded_conn) == []


def test_load_exposure_mapping():
    mapping = load_exposure_mapping(EXPOSURE_MAPPING)
    assert mapping[("sector", "Technology")] == "TECHNOLOGY"
    assert mapping[("country", "United States")] == "US"


def test_load_exposure_mapping_missing_file_returns_empty(tmp_path):
    assert load_exposure_mapping(tmp_path / "nope.csv") == {}


def test_store_exposure_maps_known_labels_and_flags_unmapped(seeded_conn):
    mapping = load_exposure_mapping(EXPOSURE_MAPPING)
    stored = store_exposure(
        seeded_conn, "DEMO-ETF-WORLD", date(2026, 6, 1), DEMO_WORLD_EXPOSURE, mapping
    )
    assert stored == 10

    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT code, weight FROM core.etp_exposure "
            "WHERE instrument_id = 'DEMO-ETF-WORLD' AND dimension = 'sector' "
            "AND label = 'Technology'"
        )
        code, weight = cur.fetchone()
        assert code == "TECHNOLOGY"
        assert weight == Decimal("0.3488")

        cur.execute(
            "SELECT code FROM core.etp_exposure WHERE dimension = 'sector' "
            "AND label = 'Other'"
        )
        assert cur.fetchone()[0] == "UNMAPPED"


def test_store_exposure_sums_to_one_per_dimension(seeded_conn):
    mapping = load_exposure_mapping(EXPOSURE_MAPPING)
    store_exposure(
        seeded_conn, "DEMO-ETF-WORLD", date(2026, 6, 1), DEMO_WORLD_EXPOSURE, mapping
    )
    with seeded_conn.cursor() as cur:
        for dimension in ("sector", "country"):
            cur.execute(
                "SELECT sum(weight) FROM core.etp_exposure "
                "WHERE instrument_id = 'DEMO-ETF-WORLD' AND dimension = %s",
                (dimension,),
            )
            total = cur.fetchone()[0]
            assert Decimal("0.99") <= total <= Decimal("1.01")


def test_store_exposure_is_idempotent_on_same_day(seeded_conn):
    mapping = load_exposure_mapping(EXPOSURE_MAPPING)
    store_exposure(
        seeded_conn, "DEMO-ETF-WORLD", date(2026, 6, 1), DEMO_WORLD_EXPOSURE, mapping
    )
    store_exposure(
        seeded_conn, "DEMO-ETF-WORLD", date(2026, 6, 1), DEMO_WORLD_EXPOSURE, mapping
    )
    with seeded_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.etp_exposure")
        assert cur.fetchone()[0] == 10


def test_refresh_exposure_stores_for_held_ucits_etf(seeded_conn):
    _buy(seeded_conn, "DEMO-ETF-WORLD", 1)
    fake = FakeExposureProvider({"IE00000000ET": DEMO_WORLD_EXPOSURE})

    result = refresh_exposure(seeded_conn, provider=fake, config_dir=EXAMPLE_CONFIG)

    assert result.refreshed == ["DEMO-ETF-WORLD"]
    assert result.skipped == []
    assert result.warnings == []
    assert fake.calls == ["IE00000000ET"]


def test_refresh_exposure_skips_unsecured_note_with_a_reason(seeded_conn):
    _buy(seeded_conn, "DEMO-ETN-GOLD", 1)
    result = refresh_exposure(
        seeded_conn, provider=FakeExposureProvider({}), config_dir=EXAMPLE_CONFIG
    )
    assert result.refreshed == []
    assert len(result.skipped) == 1
    assert "DEMO-ETN-GOLD" in result.skipped[0]
    assert "UNSECURED_NOTE" in result.skipped[0]


def test_refresh_exposure_second_run_same_day_is_a_noop(seeded_conn):
    _buy(seeded_conn, "DEMO-ETF-WORLD", 1)
    fake = FakeExposureProvider({"IE00000000ET": DEMO_WORLD_EXPOSURE})

    refresh_exposure(seeded_conn, provider=fake, config_dir=EXAMPLE_CONFIG)
    with seeded_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.etp_exposure")
        before = cur.fetchone()[0]

    refresh_exposure(seeded_conn, provider=fake, config_dir=EXAMPLE_CONFIG)
    with seeded_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.etp_exposure")
        after = cur.fetchone()[0]

    assert before == after == 10


def test_refresh_exposure_broken_provider_is_non_fatal(seeded_conn):
    _buy(seeded_conn, "DEMO-ETF-WORLD", 1)
    fake = FakeExposureProvider({}, raise_for=["IE00000000ET"])

    result = refresh_exposure(seeded_conn, provider=fake, config_dir=EXAMPLE_CONFIG)

    assert result.refreshed == []
    assert len(result.warnings) == 1
    assert "DEMO-ETF-WORLD" in result.warnings[0]

    with seeded_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.etp_exposure")
        assert cur.fetchone()[0] == 0


def test_refresh_exposure_skips_instrument_with_no_isin(seeded_conn, tmp_path):
    with seeded_conn.cursor() as cur:
        cur.execute(
            "UPDATE core.instruments SET isin = NULL WHERE instrument_id = 'DEMO-ETF-WORLD'"
        )
    seeded_conn.commit()
    _buy(seeded_conn, "DEMO-ETF-WORLD", 1)

    result = refresh_exposure(
        seeded_conn, provider=FakeExposureProvider({}), config_dir=EXAMPLE_CONFIG
    )
    assert result.refreshed == []
    assert len(result.skipped) == 1
    assert "no ISIN" in result.skipped[0]
