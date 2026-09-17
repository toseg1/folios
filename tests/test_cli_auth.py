from unittest.mock import MagicMock

from typer.testing import CliRunner

from folios import cli
from folios.cli import app
from folios.google import auth as google_auth

runner = CliRunner()


def test_auth_cli_success(monkeypatch, tmp_path):
    monkeypatch.setattr(google_auth, "TOKEN_PATH", tmp_path / "token.json")
    monkeypatch.setattr(cli.google_auth, "authenticate", MagicMock())

    result = runner.invoke(app, ["auth"])

    assert result.exit_code == 0, result.output
    assert "Authenticated" in result.output


def test_auth_cli_missing_client_secret(monkeypatch):
    def raise_missing():
        raise google_auth.MissingClientSecretError

    monkeypatch.setattr(cli.google_auth, "authenticate", raise_missing)

    result = runner.invoke(app, ["auth"])

    assert result.exit_code == 1
    assert "client_secret.json" in result.output
