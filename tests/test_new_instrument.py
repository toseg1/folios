import shutil

import pytest

from folios import new_instrument
from folios.seed import seed
from tests.test_seed import EXAMPLE_CONFIG


class FakeTickerValidator:
    def __init__(self, valid: bool = True):
        self.valid = valid
        self.checked: list[str] = []

    def has_history(self, ticker: str) -> bool:
        self.checked.append(ticker)
        return self.valid


@pytest.fixture()
def writable_config_dir(tmp_path):
    config_copy = tmp_path / "config"
    shutil.copytree(EXAMPLE_CONFIG, config_copy)
    return config_copy


@pytest.fixture()
def seeded_conn(migrated_conn, writable_config_dir):
    seed(migrated_conn, writable_config_dir)
    return migrated_conn


NEW_INSTRUMENT_FIELDS = {
    "Name": "Demo Newco",
    "Yahoo ticker": "NEWCO.PA",
    "Asset class": "EQUITY",
    "Currency": "EUR",
    "Region": "EUROPE",
}


def test_create_from_form_fields_happy_path(seeded_conn, writable_config_dir):
    validator = FakeTickerValidator(valid=True)

    result = new_instrument.create_from_form_fields(
        seeded_conn, NEW_INSTRUMENT_FIELDS, validator=validator, config_dir=writable_config_dir
    )

    assert result.error is None
    assert result.instrument_id == "NEWCO-PA"
    assert result.alias == "NEWCO-PA"
    assert validator.checked == ["NEWCO.PA"]

    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT name, currency, asset_class, price_source, yf_symbol "
            "FROM core.instruments WHERE instrument_id = 'NEWCO-PA'"
        )
        row = cur.fetchone()
    assert row == ("Demo Newco", "EUR", "EQUITY", "yfinance", "NEWCO.PA")

    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT instrument_id FROM core.instrument_aliases WHERE alias = 'NEWCO-PA'"
        )
        assert cur.fetchone() == ("NEWCO-PA",)


def test_create_from_form_fields_rejects_ticker_with_no_history(seeded_conn, writable_config_dir):
    validator = FakeTickerValidator(valid=False)

    result = new_instrument.create_from_form_fields(
        seeded_conn, NEW_INSTRUMENT_FIELDS, validator=validator, config_dir=writable_config_dir
    )

    assert result.instrument_id is None
    assert "NEWCO.PA" in result.error

    with seeded_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM core.instruments WHERE instrument_id = 'NEWCO-PA'")
        assert cur.fetchone() is None


def test_create_from_form_fields_requires_the_core_fields(seeded_conn, writable_config_dir):
    incomplete = dict(NEW_INSTRUMENT_FIELDS)
    del incomplete["Currency"]
    validator = FakeTickerValidator(valid=True)

    result = new_instrument.create_from_form_fields(
        seeded_conn, incomplete, validator=validator, config_dir=writable_config_dir
    )

    assert result.instrument_id is None
    assert "Currency" in result.error
    # Never even probes the ticker if required fields are missing.
    assert validator.checked == []


def test_create_from_form_fields_avoids_instrument_id_collision(seeded_conn, writable_config_dir):
    validator = FakeTickerValidator(valid=True)
    first = new_instrument.create_from_form_fields(
        seeded_conn, NEW_INSTRUMENT_FIELDS, validator=validator, config_dir=writable_config_dir
    )
    second_fields = dict(NEW_INSTRUMENT_FIELDS)
    second_fields["Name"] = "Demo Newco (secondary listing)"
    second = new_instrument.create_from_form_fields(
        seeded_conn, second_fields, validator=validator, config_dir=writable_config_dir
    )

    assert first.instrument_id == "NEWCO-PA"
    assert second.instrument_id == "NEWCO-PA-2"


def test_fix_ticker_updates_an_existing_manual_instrument(seeded_conn, writable_config_dir):
    # DEMO-FUND-BOND is price_source=manual in config/example/instruments.csv.
    validator = FakeTickerValidator(valid=True)

    result = new_instrument.fix_ticker(
        seeded_conn,
        "DEMO-FUND-BOND",
        "BONDFUND.PA",
        validator=validator,
        config_dir=writable_config_dir,
    )

    assert result.error is None
    assert result.instrument_id == "DEMO-FUND-BOND"

    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT yf_symbol, price_source FROM core.instruments "
            "WHERE instrument_id = 'DEMO-FUND-BOND'"
        )
        assert cur.fetchone() == ("BONDFUND.PA", "yfinance")

    # Config stays the source of truth: a rebuild must reproduce this.
    content = (writable_config_dir / "instruments.csv").read_text()
    assert "BONDFUND.PA" in content


def test_fix_ticker_rejects_unknown_instrument(seeded_conn, writable_config_dir):
    validator = FakeTickerValidator(valid=True)

    result = new_instrument.fix_ticker(
        seeded_conn,
        "NOT-A-REAL-INSTRUMENT",
        "AAPL",
        validator=validator,
        config_dir=writable_config_dir,
    )

    assert result.instrument_id is None
    assert "NOT-A-REAL-INSTRUMENT" in result.error


def test_fix_ticker_rejects_ticker_with_no_history(seeded_conn, writable_config_dir):
    validator = FakeTickerValidator(valid=False)

    result = new_instrument.fix_ticker(
        seeded_conn,
        "DEMO-FUND-BOND",
        "NOTREAL",
        validator=validator,
        config_dir=writable_config_dir,
    )

    assert result.instrument_id is None
    assert "NOTREAL" in result.error
