from typer.testing import CliRunner

from folios.cli import app
from tests.conftest import TEST_DATABASE_URL

runner = CliRunner()


def test_init_twice_is_idempotent(clean_test_db, monkeypatch):
    monkeypatch.setenv("FOLIOS_DATABASE_URL", TEST_DATABASE_URL)

    first = runner.invoke(app, ["init"])
    assert first.exit_code == 0, first.output
    assert "applied 001_schemas_and_core.sql" in first.output

    second = runner.invoke(app, ["init"])
    assert second.exit_code == 0, second.output
    assert "up to date" in second.output.lower()
