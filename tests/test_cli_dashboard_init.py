from unittest.mock import MagicMock

from typer.testing import CliRunner

from folios import cli
from folios.cli import app
from folios.metabase import MetabaseConfigError

runner = CliRunner()


def test_dashboard_init_cli_success(monkeypatch):
    monkeypatch.setattr(cli.metabase_module, "load_dashboard_config", MagicMock(return_value={}))
    monkeypatch.setattr(cli.metabase_module, "get_metabase_url", lambda: "http://localhost:3000")
    monkeypatch.setattr(cli.metabase_module, "get_api_key", lambda: "fake-key")
    monkeypatch.setattr(cli.metabase_module, "get_database_name", lambda: "folios")
    monkeypatch.setattr(cli.metabase_module, "MetabaseClient", MagicMock())
    monkeypatch.setattr(
        cli.metabase_module,
        "dashboard_init",
        MagicMock(
            return_value={"dashboard_id": 5, "card_ids": {"Holdings": 1, "Allocation": 2}}
        ),
    )

    result = runner.invoke(app, ["dashboard-init"])

    assert result.exit_code == 0, result.output
    assert "http://localhost:3000/dashboard/5" in result.output
    assert "Holdings" in result.output


def test_dashboard_init_cli_reports_missing_api_key(monkeypatch):
    monkeypatch.setattr(cli.metabase_module, "load_dashboard_config", MagicMock(return_value={}))
    monkeypatch.setattr(cli.metabase_module, "get_metabase_url", lambda: "http://localhost:3000")

    def raise_missing_key():
        raise MetabaseConfigError("Set METABASE_API_KEY in .env")

    monkeypatch.setattr(cli.metabase_module, "get_api_key", raise_missing_key)

    result = runner.invoke(app, ["dashboard-init"])

    assert result.exit_code == 1
    assert "METABASE_API_KEY" in result.output
