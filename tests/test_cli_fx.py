from datetime import date
from decimal import Decimal

from typer.testing import CliRunner

from folios import fx as fx_module
from folios.cli import app
from tests.conftest import TEST_DATABASE_URL

runner = CliRunner()


class FakeFxProvider:
    """Deterministic stand-in for FrankfurterFxProvider — no live network."""

    def fetch_rates(self, since: date, currencies: set[str]):
        return {date(2024, 3, 8): {ccy: Decimal("1.5") for ccy in currencies if ccy != "EUR"}}


def test_fx_command_stores_rates(migrated_conn, monkeypatch):
    monkeypatch.setenv("FOLIOS_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(fx_module, "FrankfurterFxProvider", FakeFxProvider)
    monkeypatch.setattr(
        fx_module, "currencies_from_config", lambda config_dir=None: {"USD"}
    )

    result = runner.invoke(app, ["fx", "--since", "2024-01-01"])
    assert result.exit_code == 0, result.output
    assert "stored 1 rate rows for USD" in result.output

    with migrated_conn.cursor() as cur:
        cur.execute(
            "SELECT rate FROM core.fx_rates WHERE quote_ccy = 'USD' AND rate_date = '2024-03-08'"
        )
        assert cur.fetchone()[0] == Decimal("1.5")
