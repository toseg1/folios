from datetime import date
from decimal import Decimal

import pytest

from folios.fx import (
    FrankfurterFxProvider,
    FxRateNotFoundError,
    currencies_from_config,
    determine_fetch_start,
    resolve_rate,
    store_rates,
)
from tests.test_seed import EXAMPLE_CONFIG


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    """A canned 404, as returned by frankfurter.dev when a requested range
    has nothing published yet (e.g. we're already fully caught up)."""

    def get(self, url, params, timeout):
        return _FakeResponse(404)


def test_currencies_from_example_config():
    currencies = currencies_from_config(EXAMPLE_CONFIG)
    assert currencies == {"EUR"}  # the example config is EUR-only


def test_currencies_from_config_collects_account_and_instrument_currencies(tmp_path):
    (tmp_path / "accounts.yml").write_text(
        "- account_id: A\n  broker: X\n  account_type: securities\n"
        "  base_currency: USD\n"
    )
    (tmp_path / "instruments.csv").write_text(
        "instrument_id,name,asset_class,currency\n"
        "I1,Instrument One,EQUITY,GBP\n"
        "I2,Instrument Two,EQUITY,EUR\n"
    )
    assert currencies_from_config(tmp_path) == {"USD", "GBP", "EUR"}


def test_determine_fetch_start_with_empty_table(migrated_conn):
    assert determine_fetch_start(migrated_conn, date(2024, 1, 1)) == date(2024, 1, 1)


def test_determine_fetch_start_resumes_after_last_stored_date(migrated_conn):
    store_rates(migrated_conn, {date(2024, 3, 10): {"USD": Decimal("1.10")}})
    # --since is a floor: an earlier value is ignored once data exists.
    assert determine_fetch_start(migrated_conn, date(2024, 1, 1)) == date(2024, 3, 11)
    # A later --since still wins if it's past the last stored date.
    assert determine_fetch_start(migrated_conn, date(2024, 4, 1)) == date(2024, 4, 1)


def test_store_rates_is_idempotent(migrated_conn):
    rates = {date(2024, 3, 10): {"USD": Decimal("1.10"), "GBP": Decimal("0.86")}}
    assert store_rates(migrated_conn, rates) == 2
    assert store_rates(migrated_conn, rates) == 2  # upsert, not duplicate

    with migrated_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.fx_rates")
        assert cur.fetchone()[0] == 2


def test_resolve_rate_eur_to_eur_never_touches_the_database():
    assert resolve_rate(None, date(2024, 3, 10), "EUR", "EUR") == Decimal("1")


def test_resolve_rate_eur_to_quote(migrated_conn):
    store_rates(migrated_conn, {date(2024, 3, 8): {"USD": Decimal("1.10")}})
    assert resolve_rate(migrated_conn, date(2024, 3, 8), "EUR", "USD") == Decimal("1.10")


def test_resolve_rate_quote_to_eur_is_the_inverse(migrated_conn):
    store_rates(migrated_conn, {date(2024, 3, 8): {"USD": Decimal("2")}})
    assert resolve_rate(migrated_conn, date(2024, 3, 8), "USD", "EUR") == Decimal("0.5")


def test_resolve_rate_cross_currency_via_eur(migrated_conn):
    store_rates(
        migrated_conn,
        {date(2024, 3, 8): {"USD": Decimal("2"), "GBP": Decimal("1")}},
    )
    # USD -> GBP = (EUR/GBP) / (EUR/USD) = 1 / 2
    assert resolve_rate(migrated_conn, date(2024, 3, 8), "USD", "GBP") == Decimal("0.5")


def test_resolve_rate_weekend_resolves_backwards_to_friday(migrated_conn):
    # Friday 2024-03-08; requesting Sunday 2024-03-10 must return Friday's rate.
    store_rates(migrated_conn, {date(2024, 3, 8): {"USD": Decimal("1.10")}})
    assert resolve_rate(migrated_conn, date(2024, 3, 10), "EUR", "USD") == Decimal("1.10")


def test_resolve_rate_raises_when_nothing_stored(migrated_conn):
    with pytest.raises(FxRateNotFoundError):
        resolve_rate(migrated_conn, date(2024, 3, 8), "EUR", "USD")


def test_fetch_rates_treats_404_as_no_new_data():
    # Reproduces a real bug: once already caught up, `since` can land past
    # the most recent published rate, and frankfurter.dev 404s instead of
    # returning an empty range — that must not raise.
    provider = FrankfurterFxProvider(session=_FakeSession())
    assert provider.fetch_rates(date(2026, 9, 17), {"USD"}) == {}
