from unittest.mock import MagicMock

from typer.testing import CliRunner

from folios import cli
from folios.cli import app
from folios.google import forms as google_forms

runner = CliRunner()


def test_form_init_cli_success(monkeypatch):
    monkeypatch.setattr(cli, "db", MagicMock())
    monkeypatch.setattr(
        cli.google_forms,
        "form_init",
        MagicMock(
            return_value={
                "form_id": "abc123",
                "responder_uri": "https://forms.example/abc123",
                "sheet_id": "sheet456",
            }
        ),
    )

    result = runner.invoke(app, ["form-init"])

    assert result.exit_code == 0, result.output
    assert "abc123" in result.output
    assert "sheet456" in result.output


def test_form_sync_cli_success(monkeypatch):
    monkeypatch.setattr(cli, "db", MagicMock())
    monkeypatch.setattr(
        cli.google_forms,
        "form_sync",
        MagicMock(return_value={"form_id": "abc123", "updated_items": 4}),
    )

    result = runner.invoke(app, ["form-sync"])

    assert result.exit_code == 0, result.output
    assert "4" in result.output
    assert "abc123" in result.output


def test_form_sync_cli_not_initialized(monkeypatch):
    monkeypatch.setattr(cli, "db", MagicMock())

    def raise_not_initialized(conn):
        raise google_forms.FormNotInitializedError

    monkeypatch.setattr(cli.google_forms, "form_sync", raise_not_initialized)

    result = runner.invoke(app, ["form-sync"])

    assert result.exit_code == 1
    assert "form-init" in result.output
