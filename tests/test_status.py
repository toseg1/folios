from datetime import date, timedelta
from decimal import Decimal

import pytest

from folios.exposure import store_exposure
from folios.seed import seed
from folios.status import (
    check_missing_tickers,
    check_stale_manual_valuations,
    check_unmapped_exposure_codes,
)
from folios.valuations import load_valuations
from tests.test_seed import EXAMPLE_CONFIG


@pytest.fixture()
def seeded_conn(migrated_conn):
    seed(migrated_conn, EXAMPLE_CONFIG)
    return migrated_conn


def test_flags_instrument_with_no_valuation_at_all(seeded_conn):
    findings = check_stale_manual_valuations(seeded_conn)
    texts = [str(f) for f in findings]
    assert any("DEMO-FUND-BOND" in t and "no manual valuation" in t for t in texts)
    assert any("DEMO-BOND-2030" in t and "no manual valuation" in t for t in texts)


def test_flags_valuation_older_than_threshold(seeded_conn, tmp_path):
    old_date = date.today() - timedelta(days=150)
    path = tmp_path / "valuations.csv"
    path.write_text(f"date,symbol,price,currency\n{old_date.isoformat()},FUNDBOND,215,EUR\n")
    load_valuations(seeded_conn, path)

    findings = [str(f) for f in check_stale_manual_valuations(seeded_conn)]
    assert any("DEMO-FUND-BOND" in t and "150 days ago" in t for t in findings)


def test_does_not_flag_a_recent_valuation(seeded_conn, tmp_path):
    recent = date.today() - timedelta(days=10)
    path = tmp_path / "valuations.csv"
    path.write_text(f"date,symbol,price,currency\n{recent.isoformat()},FUNDBOND,215,EUR\n")
    load_valuations(seeded_conn, path)

    findings = [str(f) for f in check_stale_manual_valuations(seeded_conn)]
    assert not any("DEMO-FUND-BOND" in t for t in findings)


def test_respects_custom_max_age(seeded_conn, tmp_path):
    ten_days_ago = date.today() - timedelta(days=10)
    path = tmp_path / "valuations.csv"
    path.write_text(
        f"date,symbol,price,currency\n{ten_days_ago.isoformat()},FUNDBOND,215,EUR\n"
    )
    load_valuations(seeded_conn, path)

    findings = [
        str(f) for f in check_stale_manual_valuations(seeded_conn, max_age_days=5)
    ]
    assert any("DEMO-FUND-BOND" in t for t in findings)


def test_check_unmapped_exposure_codes(seeded_conn):
    store_exposure(
        seeded_conn,
        "DEMO-ETF-WORLD",
        date(2026, 6, 1),
        {"sector": [("Weird New Sector", Decimal("0.10"))]},
        mapping={},  # nothing mapped -> UNMAPPED
    )
    findings = [str(f) for f in check_unmapped_exposure_codes(seeded_conn)]
    assert len(findings) == 1
    assert "DEMO-ETF-WORLD" in findings[0]
    assert "Weird New Sector" in findings[0]
    assert "exposure_mapping.csv" in findings[0]


def test_no_unmapped_exposure_codes_when_none_stored(seeded_conn):
    assert check_unmapped_exposure_codes(seeded_conn) == []


def test_check_missing_tickers_flags_yfinance_instrument_without_symbol(seeded_conn):
    with seeded_conn.cursor() as cur:
        cur.execute(
            "UPDATE core.instruments SET yf_symbol = NULL, price_source = 'yfinance' "
            "WHERE instrument_id = 'DEMO-SHARE'"
        )
    seeded_conn.commit()

    findings = [str(f) for f in check_missing_tickers(seeded_conn)]
    assert any("DEMO-SHARE" in t and "fix-ticker" in t for t in findings)


def test_check_missing_tickers_ignores_manual_price_source(seeded_conn):
    # DEMO-FUND-BOND is price_source=manual with no yf_symbol by design.
    findings = [str(f) for f in check_missing_tickers(seeded_conn)]
    assert not any("DEMO-FUND-BOND" in t for t in findings)


def test_check_missing_tickers_clean_when_all_yfinance_instruments_have_a_symbol(seeded_conn):
    assert check_missing_tickers(seeded_conn) == []
