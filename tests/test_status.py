from datetime import date, timedelta

import pytest

from folios.seed import seed
from folios.status import check_stale_manual_valuations
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
