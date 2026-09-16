from datetime import date, timedelta
from pathlib import Path

from typer.testing import CliRunner

from folios.cli import app
from folios.seed import seed
from tests.conftest import TEST_DATABASE_URL
from tests.test_seed import EXAMPLE_CONFIG

runner = CliRunner()


def test_value_cli_loads_and_status_flags_stale(migrated_conn, monkeypatch, tmp_path):
    seed(migrated_conn, EXAMPLE_CONFIG)
    monkeypatch.setenv("FOLIOS_DATABASE_URL", TEST_DATABASE_URL)

    recent = (date.today() - timedelta(days=10)).isoformat()
    path: Path = tmp_path / "valuations.csv"
    path.write_text(f"date,symbol,price,currency\n{recent},FUNDBOND,215.00,EUR\n")

    result = runner.invoke(app, ["value", str(path)])
    assert result.exit_code == 0, result.output
    assert "stored 1" in result.output

    status_result = runner.invoke(app, ["status"])
    assert status_result.exit_code == 0, status_result.output
    # DEMO-FUND-BOND now has a valuation; DEMO-BOND-2030 still has none.
    assert "DEMO-FUND-BOND" not in status_result.output
    assert "DEMO-BOND-2030" in status_result.output
    assert "no manual valuation" in status_result.output


def test_value_cli_rejects_bad_row(migrated_conn, monkeypatch, tmp_path):
    seed(migrated_conn, EXAMPLE_CONFIG)
    monkeypatch.setenv("FOLIOS_DATABASE_URL", TEST_DATABASE_URL)

    path = tmp_path / "valuations.csv"
    path.write_text("date,symbol,price,currency\n2026-03-31,NOT_A_SYMBOL,1,EUR\n")

    result = runner.invoke(app, ["value", str(path)])
    assert result.exit_code == 1
    assert "does not resolve" in result.output


def test_status_cli_nothing_to_flag_when_no_manual_instruments(migrated_conn, monkeypatch):
    # No config seeded at all -> no instruments, so nothing to flag.
    monkeypatch.setenv("FOLIOS_DATABASE_URL", TEST_DATABASE_URL)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "nothing to flag" in result.output
