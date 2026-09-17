from unittest.mock import MagicMock

from typer.testing import CliRunner

from folios import cli
from folios.cli import app
from folios.google import forms as google_forms
from folios.google.sheets import PullResult

runner = CliRunner()


def test_pull_cli_success(monkeypatch):
    monkeypatch.setattr(cli, "db", MagicMock())
    monkeypatch.setattr(
        cli.google_sheets,
        "pull",
        MagicMock(
            return_value=PullResult(
                responses_seen=2,
                transactions_written=1,
                valuations_written=1,
            )
        ),
    )

    result = runner.invoke(app, ["pull"])

    assert result.exit_code == 0, result.output
    assert "2 new response" in result.output
    assert "1 transaction row" in result.output
    assert "1 valuation row" in result.output


def test_pull_cli_reports_errors(monkeypatch):
    monkeypatch.setattr(cli, "db", MagicMock())
    monkeypatch.setattr(
        cli.google_sheets,
        "pull",
        MagicMock(
            return_value=PullResult(
                responses_seen=1,
                errors=["r1: account=BAD does not exist"],
            )
        ),
    )

    result = runner.invoke(app, ["pull"])

    assert result.exit_code == 0, result.output
    assert "account=BAD does not exist" in result.output


def test_pull_cli_not_initialized(monkeypatch):
    monkeypatch.setattr(cli, "db", MagicMock())

    def raise_not_initialized(conn):
        raise google_forms.FormNotInitializedError

    monkeypatch.setattr(cli.google_sheets, "pull", raise_not_initialized)

    result = runner.invoke(app, ["pull"])

    assert result.exit_code == 1
    assert "form-init" in result.output
