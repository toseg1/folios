from datetime import date
from decimal import Decimal

import pytest

from folios.exposure import (
    etps_held,
    load_exposure_mapping,
    parse_basics,
    refresh_exposure,
    store_basics,
    store_exposure,
)
from folios.seed import seed
from tests.test_seed import EXAMPLE_CONFIG

EXPOSURE_MAPPING = EXAMPLE_CONFIG / "exposure_mapping.csv"


class FakeExposureProvider:
    def __init__(self, by_isin=None, raise_for=(), basics_by_isin=None, basics_raise_for=()):
        self._by_isin = by_isin or {}
        self._raise_for = set(raise_for)
        self.calls: list[str] = []
        self.basics_calls: list[str] = []
        # refresh_exposure() only calls fetch_basics if the provider
        # actually has one (getattr(..., None) gate) — same convention as
        # fetch_top_holdings, so a plain FakeExposureProvider() with no
        # basics configured behaves exactly as it did before this method
        # existed, rather than raising on an unconfigured isin.
        if basics_by_isin is not None or basics_raise_for:
            self._basics_by_isin = basics_by_isin or {}
            self._basics_raise_for = set(basics_raise_for)
            self.fetch_basics = self._fetch_basics

    def fetch_exposure(self, isin: str):
        self.calls.append(isin)
        if isin in self._raise_for:
            raise RuntimeError("justETF page changed, could not parse")
        return self._by_isin[isin]

    def _fetch_basics(self, isin: str):
        self.basics_calls.append(isin)
        if isin in self._basics_raise_for:
            raise RuntimeError("justETF page changed, could not parse")
        return self._basics_by_isin[isin]


# Confirmed live against a real ISIN (EUNL / IE00B4L5Y983) — see
# docs/folios-data-model-review.md and migrations/007's comments.
EUNL_BASICS_RAW = {
    "Index": "MSCI World",
    "Investment focus": "Equity, World",
    "Fund size": "EUR 127,272 m",
    "Total expense ratio": "0.20% p.a.",
    "Replication": "Physical (Optimized sampling)",
    "Legal structure": "ETF",
    "Investment approach": "Long-only",
    "Sustainability": "No",
    "Fund currency": "USD",
    "Currency risk": "Currency unhedged",
    "Volatility 1 year (in EUR)": "10.73%",
    "Inception/ Listing Date": "25 September 2009",
    "Distribution policy": "Accumulating",
    "Distribution frequency": "-",
    "Fund domicile": "Ireland",
    "Fund Provider": "iShares",
}


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
    _buy(seeded_conn, "DEMO-SHARE", 2)  # not an exchange-traded fund, must not appear
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
    assert "is_ucits=False" in result.skipped[0]


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


def test_parse_basics_matches_confirmed_live_shape():
    mapping = load_exposure_mapping(EXPOSURE_MAPPING)
    warnings: list[str] = []

    parsed = parse_basics(EUNL_BASICS_RAW, mapping, warnings)

    assert warnings == []
    assert parsed == {
        "benchmark_index": "MSCI World",
        "investment_focus": "Equity, World",
        "fund_size": Decimal("127272000000"),
        "ongoing_charges": Decimal("0.0020"),
        "replication_method": "PHYSICAL_SAMPLED",
        "investment_approach": "Long-only",
        "sustainability": "No",
        "fund_currency": "USD",
        "currency_risk": "Currency unhedged",
        "volatility_1y_eur": Decimal("0.1073"),
        "inception_date": date(2009, 9, 25),
        "distribution_policy": "ACCUMULATING",
        "distribution_frequency": None,
        "domicile_country": "IE",
        "issuer": "iShares",
    }


def test_parse_basics_never_reads_justetfs_own_legal_structure_field():
    # justETF's "Legal structure" ("ETF") is the wrapper-type concept
    # this schema already calls instrument_type — parse_basics must not
    # read it into anything.
    mapping = load_exposure_mapping(EXPOSURE_MAPPING)
    parsed = parse_basics(EUNL_BASICS_RAW, mapping, [])
    assert "legal_structure" not in parsed
    assert "instrument_type" not in parsed


def test_parse_basics_unmapped_domicile_leaves_blank_and_warns():
    raw = {**EUNL_BASICS_RAW, "Fund domicile": "Atlantis"}
    warnings: list[str] = []

    parsed = parse_basics(raw, load_exposure_mapping(EXPOSURE_MAPPING), warnings)

    assert parsed["domicile_country"] is None
    assert len(warnings) == 1
    assert "Atlantis" in warnings[0]


def test_store_basics_updates_fund_and_instrument_columns_without_touching_manual_ones(
    seeded_conn,
):
    mapping = load_exposure_mapping(EXPOSURE_MAPPING)
    parsed = parse_basics(EUNL_BASICS_RAW, mapping, [])

    store_basics(seeded_conn, "DEMO-ETF-WORLD", parsed)

    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT ongoing_charges, fund_currency, custodian "
            "FROM core.instrument_fund WHERE instrument_id = 'DEMO-ETF-WORLD'"
        )
        ongoing_charges, fund_currency, custodian = cur.fetchone()
        cur.execute(
            "SELECT currency, domicile_country, issuer FROM core.instruments "
            "WHERE instrument_id = 'DEMO-ETF-WORLD'"
        )
        currency, domicile_country, issuer = cur.fetchone()

    assert ongoing_charges == Decimal("0.0020")
    assert fund_currency == "USD"
    # The regression this phase exists to prevent: the fund's own NAV
    # currency (USD) must never land on the instrument's trading currency.
    assert currency == "EUR"
    assert domicile_country == "IE"
    assert issuer == "iShares"
    # custodian is manual — untouched by store_basics.
    assert custodian == "Demo Depositary Bank"


def test_store_basics_never_nulls_an_existing_value_on_a_partial_fetch(seeded_conn):
    store_basics(
        seeded_conn,
        "DEMO-ETF-WORLD",
        {
            "benchmark_index": None, "investment_focus": None, "fund_size": None,
            "ongoing_charges": None, "replication_method": None,
            "investment_approach": None, "sustainability": None, "fund_currency": None,
            "currency_risk": None, "volatility_1y_eur": None, "inception_date": None,
            "distribution_policy": None, "distribution_frequency": None,
            "domicile_country": None, "issuer": None,
        },
    )
    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT ongoing_charges FROM core.instrument_fund "
            "WHERE instrument_id = 'DEMO-ETF-WORLD'"
        )
        assert cur.fetchone()[0] == Decimal("0.0020")


def test_refresh_exposure_also_refreshes_basics(seeded_conn):
    _buy(seeded_conn, "DEMO-ETF-WORLD", 1)
    fake = FakeExposureProvider(
        {"IE00000000ET": DEMO_WORLD_EXPOSURE},
        basics_by_isin={"IE00000000ET": EUNL_BASICS_RAW},
    )

    result = refresh_exposure(seeded_conn, provider=fake, config_dir=EXAMPLE_CONFIG)

    assert result.warnings == []
    assert fake.basics_calls == ["IE00000000ET"]
    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT fund_currency FROM core.instrument_fund "
            "WHERE instrument_id = 'DEMO-ETF-WORLD'"
        )
        assert cur.fetchone()[0] == "USD"


def test_refresh_exposure_basics_failure_is_non_fatal_and_keeps_exposure_refresh(seeded_conn):
    _buy(seeded_conn, "DEMO-ETF-WORLD", 1)
    fake = FakeExposureProvider(
        {"IE00000000ET": DEMO_WORLD_EXPOSURE},
        basics_by_isin={},
        basics_raise_for=["IE00000000ET"],
    )

    result = refresh_exposure(seeded_conn, provider=fake, config_dir=EXAMPLE_CONFIG)

    assert result.refreshed == ["DEMO-ETF-WORLD"]  # sector/country still succeeded
    assert len(result.warnings) == 1
    assert "basics refresh failed" in result.warnings[0]
