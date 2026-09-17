from unittest.mock import MagicMock

from typer.testing import CliRunner

from folios import cli
from folios.cli import app
from folios.new_instrument import FixTickerResult

runner = CliRunner()


def test_fix_ticker_cli_success(monkeypatch):
    monkeypatch.setattr(cli, "db", MagicMock())
    monkeypatch.setattr(
        cli.new_instrument_module,
        "fix_ticker",
        MagicMock(return_value=FixTickerResult(instrument_id="DEMO-FUND-BOND")),
    )

    result = runner.invoke(app, ["fix-ticker", "DEMO-FUND-BOND", "BONDFUND.PA"])

    assert result.exit_code == 0, result.output
    assert "DEMO-FUND-BOND" in result.output
    assert "BONDFUND.PA" in result.output


def test_fix_ticker_cli_reports_error(monkeypatch):
    monkeypatch.setattr(cli, "db", MagicMock())
    monkeypatch.setattr(
        cli.new_instrument_module,
        "fix_ticker",
        MagicMock(
            return_value=FixTickerResult(
                error="ticker 'NOTREAL' has no price history on Yahoo Finance"
            )
        ),
    )

    result = runner.invoke(app, ["fix-ticker", "DEMO-FUND-BOND", "NOTREAL"])

    assert result.exit_code == 1
    assert "NOTREAL" in result.output
