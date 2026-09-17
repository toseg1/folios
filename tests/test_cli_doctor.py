from unittest.mock import MagicMock

from typer.testing import CliRunner

from folios import cli
from folios.cli import app
from folios.doctor import Check

runner = CliRunner()


def test_doctor_cli_exits_zero_when_everything_ok(monkeypatch):
    monkeypatch.setattr(
        cli.doctor_module,
        "run_doctor",
        MagicMock(return_value=[Check("docker", True, "Docker is running")]),
    )

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "Docker is running" in result.output


def test_doctor_cli_exits_nonzero_when_something_failed(monkeypatch):
    monkeypatch.setattr(
        cli.doctor_module,
        "run_doctor",
        MagicMock(
            return_value=[
                Check("docker", True, "Docker is running"),
                Check("google credentials", False, "run `folios auth`"),
            ]
        ),
    )

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "folios auth" in result.output
