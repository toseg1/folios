from datetime import date
from decimal import Decimal

from typer.testing import CliRunner

from folios import prices as prices_module
from folios.cli import app
from folios.seed import seed
from tests.conftest import TEST_DATABASE_URL
from tests.test_prices import FakePriceProvider
from tests.test_seed import EXAMPLE_CONFIG

runner = CliRunner()


def test_prices_cli_stores_rows(migrated_conn, monkeypatch):
    seed(migrated_conn, EXAMPLE_CONFIG)
    monkeypatch.setenv("FOLIOS_DATABASE_URL", TEST_DATABASE_URL)

    with migrated_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, quantity, price, net_amount, currency)
            VALUES ('csv:t:1', 'h1', 'DEMO-BROKER-CTO', '2026-01-05', 'BUY',
                    'DEMO-SHARE', 1, 100, -100, 'EUR')
            """
        )
    migrated_conn.commit()

    fake = FakePriceProvider(
        {"DEMO-SHARE": {date(2026, 1, 6): (Decimal("101"), Decimal("101"))}}
    )
    monkeypatch.setattr(prices_module, "YFinancePriceProvider", lambda: fake)

    result = runner.invoke(app, ["prices"])
    assert result.exit_code == 0, result.output
    assert "stored 1 price rows" in result.output
