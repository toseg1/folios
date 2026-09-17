from decimal import Decimal

from typer.testing import CliRunner

from folios import exposure as exposure_module
from folios.cli import app
from folios.seed import seed
from tests.conftest import TEST_DATABASE_URL
from tests.test_exposure import DEMO_WORLD_EXPOSURE, FakeExposureProvider
from tests.test_seed import EXAMPLE_CONFIG

runner = CliRunner()


def test_exposure_cli_without_refresh_is_a_noop(migrated_conn, monkeypatch):
    monkeypatch.setenv("FOLIOS_DATABASE_URL", TEST_DATABASE_URL)
    result = runner.invoke(app, ["exposure"])
    assert result.exit_code == 0
    assert "nothing to do" in result.output


def test_exposure_cli_refresh(migrated_conn, monkeypatch):
    seed(migrated_conn, EXAMPLE_CONFIG)
    monkeypatch.setenv("FOLIOS_DATABASE_URL", TEST_DATABASE_URL)

    with migrated_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, quantity, price, net_amount, currency)
            VALUES ('csv:t:1', 'h1', 'DEMO-BROKER-CTO', '2026-01-05', 'BUY',
                    'DEMO-ETF-WORLD', 1, 100, -100, 'EUR')
            """
        )
    migrated_conn.commit()

    fake = FakeExposureProvider({"IE00000000ET": DEMO_WORLD_EXPOSURE})
    monkeypatch.setattr(exposure_module, "StockdexExposureProvider", lambda: fake)
    monkeypatch.setattr(exposure_module, "CONFIG_DIR", EXAMPLE_CONFIG)

    result = runner.invoke(app, ["exposure", "--refresh"])
    assert result.exit_code == 0, result.output
    assert "refreshed DEMO-ETF-WORLD" in result.output

    with migrated_conn.cursor() as cur:
        cur.execute("SELECT sum(weight) FROM core.etp_exposure WHERE dimension = 'sector'")
        total = cur.fetchone()[0]
    assert Decimal("0.99") <= total <= Decimal("1.01")
