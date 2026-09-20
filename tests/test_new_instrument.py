import shutil
from decimal import Decimal

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


class FakeInfoProvider:
    """Stands in for YFinanceInfoProvider — never hits yfinance for real."""

    def __init__(self, info: dict | None = None):
        self.info = info or {}
        self.requested: list[str] = []

    def fetch_info(self, ticker: str) -> dict:
        self.requested.append(ticker)
        return self.info


class FakeBasicsProvider:
    """Stands in for exposure.StockdexExposureProvider — never hits
    justETF for real."""

    def __init__(self, basics: dict | None = None):
        self.basics = basics or {}
        self.requested: list[str] = []

    def fetch_basics(self, isin: str) -> dict:
        self.requested.append(isin)
        return self.basics


# Confirmed live against a real ISIN (EUNL / IE00B4L5Y983) — same fixture
# as tests/test_exposure.py's EUNL_BASICS_RAW.
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
}


def test_create_from_form_fields_happy_path(seeded_conn, writable_config_dir):
    validator = FakeTickerValidator(valid=True)

    result = new_instrument.create_from_form_fields(
        seeded_conn,
        NEW_INSTRUMENT_FIELDS,
        validator=validator,
        config_dir=writable_config_dir,
        info_provider=FakeInfoProvider(),
    )

    assert result.error is None
    assert result.instrument_id == "NEWCO-PA"
    assert result.alias == "Demo Newco - NEWCO-PA"
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
            "SELECT instrument_id FROM core.instrument_aliases "
            "WHERE alias = 'Demo Newco - NEWCO-PA'"
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
        seeded_conn,
        NEW_INSTRUMENT_FIELDS,
        validator=validator,
        config_dir=writable_config_dir,
        info_provider=FakeInfoProvider(),
    )
    second_fields = dict(NEW_INSTRUMENT_FIELDS)
    second_fields["Name"] = "Demo Newco (secondary listing)"
    second = new_instrument.create_from_form_fields(
        seeded_conn,
        second_fields,
        validator=validator,
        config_dir=writable_config_dir,
        info_provider=FakeInfoProvider(),
    )

    assert first.instrument_id == "NEWCO-PA"
    assert second.instrument_id == "NEWCO-PA-2"


def test_create_from_form_fields_writes_no_subtype_row_for_equity(
    seeded_conn, writable_config_dir
):
    validator = FakeTickerValidator(valid=True)
    new_instrument.create_from_form_fields(
        seeded_conn,
        NEW_INSTRUMENT_FIELDS,
        validator=validator,
        config_dir=writable_config_dir,
        info_provider=FakeInfoProvider(),
    )

    assert not (writable_config_dir / "instruments_fund.csv").read_text().count("NEWCO-PA")
    assert not (writable_config_dir / "instruments_bond.csv").read_text().count("NEWCO-PA")
    assert not (writable_config_dir / "instruments_crypto.csv").read_text().count("NEWCO-PA")

    with seeded_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM core.instrument_fund WHERE instrument_id = 'NEWCO-PA'")
        assert cur.fetchone() is None


def test_create_from_form_fields_writes_fund_subtype_row(seeded_conn, writable_config_dir):
    fields = {
        **NEW_INSTRUMENT_FIELDS,
        "Asset class": "ETP",
        "Legal structure": "UCITS_FUND",
        "SRI": "4",
    }
    validator = FakeTickerValidator(valid=True)

    result = new_instrument.create_from_form_fields(
        seeded_conn, fields, validator=validator, config_dir=writable_config_dir
    )

    assert result.error is None
    fund_csv = (writable_config_dir / "instruments_fund.csv").read_text()
    assert "NEWCO-PA,UCITS_FUND" in fund_csv
    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT legal_structure, sri FROM core.instrument_fund "
            "WHERE instrument_id = 'NEWCO-PA'"
        )
        assert cur.fetchone() == ("UCITS_FUND", 4)


def test_create_from_form_fields_enriches_etp_via_basics_provider(
    seeded_conn, writable_config_dir
):
    fields = {
        **NEW_INSTRUMENT_FIELDS,
        "Asset class": "ETP",
        "Legal structure": "UCITS_FUND",
        "ISIN": "IE00B4L5Y983",
    }
    validator = FakeTickerValidator(valid=True)
    basics_provider = FakeBasicsProvider(EUNL_BASICS_RAW)

    result = new_instrument.create_from_form_fields(
        seeded_conn,
        fields,
        validator=validator,
        config_dir=writable_config_dir,
        basics_provider=basics_provider,
    )

    assert result.error is None
    assert basics_provider.requested == ["IE00B4L5Y983"]
    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT ongoing_charges, replication_method, fund_currency, "
            "investment_approach, distribution_frequency "
            "FROM core.instrument_fund WHERE instrument_id = 'NEWCO-PA'"
        )
        row = cur.fetchone()
        cur.execute(
            "SELECT currency, domicile_country, issuer FROM core.instruments "
            "WHERE instrument_id = 'NEWCO-PA'"
        )
        currency, domicile_country, issuer = cur.fetchone()

    assert row[0] == Decimal("0.0020")
    assert row[1] == "PHYSICAL_SAMPLED"
    assert row[2] == "USD"
    assert row[3] == "Long-only"
    assert row[4] is None  # "-" parsed to blank, not the literal dash
    # The regression this phase exists to prevent: the trading currency
    # you typed (EUR) must never be overwritten by the fund's own NAV
    # currency (USD) from justETF.
    assert currency == "EUR"
    assert domicile_country == "IE"
    assert issuer == "iShares"


def test_create_from_form_fields_etp_without_isin_warns_but_still_creates(
    seeded_conn, writable_config_dir
):
    fields = {
        **NEW_INSTRUMENT_FIELDS,
        "Asset class": "ETP",
        "Legal structure": "UCITS_FUND",
    }
    validator = FakeTickerValidator(valid=True)
    basics_provider = FakeBasicsProvider(EUNL_BASICS_RAW)

    result = new_instrument.create_from_form_fields(
        seeded_conn,
        fields,
        validator=validator,
        config_dir=writable_config_dir,
        basics_provider=basics_provider,
    )

    assert result.error is None
    assert basics_provider.requested == []  # never called without an ISIN
    assert any("ISIN" in w for w in result.warnings)


def test_create_from_form_fields_writes_bond_subtype_row(seeded_conn, writable_config_dir):
    fields = {
        **NEW_INSTRUMENT_FIELDS,
        "Asset class": "BOND",
        "Coupon rate": "0.035",
        "Is callable": "false",
    }
    validator = FakeTickerValidator(valid=True)

    result = new_instrument.create_from_form_fields(
        seeded_conn, fields, validator=validator, config_dir=writable_config_dir
    )

    assert result.error is None
    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT coupon_rate, is_callable FROM core.instrument_bond "
            "WHERE instrument_id = 'NEWCO-PA'"
        )
        row = cur.fetchone()
    assert row[0] == Decimal("0.035000")
    assert row[1] is False


def test_create_from_form_fields_writes_crypto_subtype_row(seeded_conn, writable_config_dir):
    fields = {
        **NEW_INSTRUMENT_FIELDS,
        "Asset class": "CRYPTO",
        "Chain": "Ethereum",
        "Is stablecoin": "false",
    }
    validator = FakeTickerValidator(valid=True)

    result = new_instrument.create_from_form_fields(
        seeded_conn, fields, validator=validator, config_dir=writable_config_dir
    )

    assert result.error is None
    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT chain, is_stablecoin FROM core.instrument_crypto "
            "WHERE instrument_id = 'NEWCO-PA'"
        )
        assert cur.fetchone() == ("Ethereum", False)


def test_create_from_form_fields_enriches_equity_via_info_provider(
    seeded_conn, writable_config_dir
):
    (writable_config_dir / "exposure_mapping.csv").write_text(
        "dimension,source_label,code\n"
        "sector,Technology,TECHNOLOGY\n"
        "country,United States,US\n"
    )
    validator = FakeTickerValidator(valid=True)
    info_provider = FakeInfoProvider(
        {
            "sector": "Technology",
            "industry": "Software",  # deliberately unmapped
            "country": "United States",
            "longBusinessSummary": "Makes software.",
        }
    )

    result = new_instrument.create_from_form_fields(
        seeded_conn,
        NEW_INSTRUMENT_FIELDS,
        validator=validator,
        config_dir=writable_config_dir,
        info_provider=info_provider,
    )

    assert result.error is None
    assert info_provider.requested == ["NEWCO.PA"]
    assert any("industry" in w for w in result.warnings)

    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT sector, industry, domicile_country, description "
            "FROM core.instruments WHERE instrument_id = 'NEWCO-PA'"
        )
        row = cur.fetchone()
    # sector/country mapped via exposure_mapping.csv; industry has no
    # mapping row, so it's left NULL rather than seeded with an unknown
    # dimension code (which would hard-fail seed.seed()).
    assert row == ("TECHNOLOGY", None, "US", "Makes software.")


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
