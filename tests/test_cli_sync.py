from unittest.mock import MagicMock

from typer.testing import CliRunner

from folios import cli
from folios.cli import app
from folios.sync import StepResult, SyncResult

runner = CliRunner()


def test_sync_cli_success(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "db", MagicMock())
    clean_result = SyncResult(steps=[StepResult("pull", True, "0 new response(s)")])
    monkeypatch.setattr(cli.sync_module, "run_sync", MagicMock(return_value=clean_result))
    log_path = tmp_path / "sync.log"
    monkeypatch.setattr(cli.sync_module, "append_log", MagicMock(return_value=log_path))

    result = runner.invoke(app, ["sync"])

    assert result.exit_code == 0, result.output
    assert str(log_path) in result.output


def test_sync_cli_exits_nonzero_when_a_step_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "db", MagicMock())
    failing_result = SyncResult(
        steps=[StepResult("load", False, "1 error", details=["something broke"])]
    )
    monkeypatch.setattr(cli.sync_module, "run_sync", MagicMock(return_value=failing_result))
    log_path = tmp_path / "sync.log"
    monkeypatch.setattr(cli.sync_module, "append_log", MagicMock(return_value=log_path))

    result = runner.invoke(app, ["sync"])

    assert result.exit_code == 1
    assert "something broke" in result.output
