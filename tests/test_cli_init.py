from pathlib import Path

from typer.testing import CliRunner

from folios import seed
from folios.cli import app
from tests.conftest import TEST_DATABASE_URL

runner = CliRunner()

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "example"


def test_init_twice_is_idempotent(clean_test_db, monkeypatch):
    monkeypatch.setenv("FOLIOS_DATABASE_URL", TEST_DATABASE_URL)

    first = runner.invoke(app, ["init"])
    assert first.exit_code == 0, first.output
    assert "applied 001_schemas_and_core.sql" in first.output

    second = runner.invoke(app, ["init"])
    assert second.exit_code == 0, second.output
    assert "up to date" in second.output.lower()


def test_init_seeds_accounts_instruments_and_dimensions(clean_test_db, monkeypatch):
    monkeypatch.setenv("FOLIOS_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(seed, "CONFIG_DIR", EXAMPLE_CONFIG)

    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert "seeded 10 accounts" in result.output
    assert "seeded 12 instruments" in result.output

    with clean_test_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.accounts")
        assert cur.fetchone()[0] == 10
        cur.execute("SELECT count(*) FROM core.instruments")
        assert cur.fetchone()[0] == 12
        cur.execute("SELECT count(*) FROM core.dimensions")
        assert cur.fetchone()[0] > 0
