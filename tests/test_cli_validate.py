from pathlib import Path

from typer.testing import CliRunner

from folios.cli import app
from folios.seed import seed
from tests.conftest import TEST_DATABASE_URL
from tests.test_seed import EXAMPLE_CONFIG

runner = CliRunner()
SAMPLES_DIR = Path(__file__).resolve().parents[1] / "data" / "samples"


def test_validate_cli_good_fixture_exits_zero(migrated_conn, monkeypatch):
    seed(migrated_conn, EXAMPLE_CONFIG)
    monkeypatch.setenv("FOLIOS_DATABASE_URL", TEST_DATABASE_URL)

    result = runner.invoke(
        app, ["validate", str(SAMPLES_DIR / "transactions_good.csv")]
    )
    assert result.exit_code == 0, result.output
    assert "no errors" in result.output


def test_validate_cli_bad_fixture_exits_nonzero(migrated_conn, monkeypatch):
    seed(migrated_conn, EXAMPLE_CONFIG)
    monkeypatch.setenv("FOLIOS_DATABASE_URL", TEST_DATABASE_URL)

    result = runner.invoke(
        app, ["validate", str(SAMPLES_DIR / "transactions_bad.csv")]
    )
    assert result.exit_code == 1
    assert "ERROR" in result.output
