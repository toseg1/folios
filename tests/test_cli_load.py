from pathlib import Path

from typer.testing import CliRunner

from folios import loader as loader_module
from folios import seed as seed_module
from folios.cli import app
from folios.seed import seed
from tests.conftest import TEST_DATABASE_URL
from tests.test_seed import EXAMPLE_CONFIG

runner = CliRunner()
GOOD_FIXTURE = (
    Path(__file__).resolve().parents[1] / "data" / "samples" / "transactions_good.csv"
)


def test_load_cli_inserts_then_skips(migrated_conn, monkeypatch):
    seed(migrated_conn, EXAMPLE_CONFIG)
    monkeypatch.setenv("FOLIOS_DATABASE_URL", TEST_DATABASE_URL)

    first = runner.invoke(app, ["load", str(GOOD_FIXTURE)])
    assert first.exit_code == 0, first.output
    assert "inserted 5" in first.output

    second = runner.invoke(app, ["load", str(GOOD_FIXTURE)])
    assert second.exit_code == 0, second.output
    assert "skipped 5" in second.output


def test_rebuild_cli(migrated_conn, monkeypatch, tmp_path):
    monkeypatch.setenv("FOLIOS_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(seed_module, "CONFIG_DIR", EXAMPLE_CONFIG)

    data_dir = tmp_path / "manual"
    data_dir.mkdir()
    (data_dir / "transactions.csv").write_text(GOOD_FIXTURE.read_text())
    monkeypatch.setattr(loader_module, "DEFAULT_DATA_DIR", data_dir)

    result = runner.invoke(app, ["rebuild"])
    assert result.exit_code == 0, result.output
    assert "inserted 5" in result.output

    with migrated_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.transactions")
        assert cur.fetchone()[0] == 5
