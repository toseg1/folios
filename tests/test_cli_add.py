from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from folios import validate as validate_module
from folios.cli import app
from folios.seed import seed
from tests.conftest import TEST_DATABASE_URL
from tests.test_seed import EXAMPLE_CONFIG

runner = CliRunner()


def _prepare(migrated_conn, monkeypatch, tmp_path) -> Path:
    seed(migrated_conn, EXAMPLE_CONFIG)
    monkeypatch.setenv("FOLIOS_DATABASE_URL", TEST_DATABASE_URL)
    entries_path = tmp_path / "transactions.csv"
    monkeypatch.setattr(validate_module, "DEFAULT_ENTRIES_PATH", entries_path)
    return entries_path


def test_add_buy_end_to_end(migrated_conn, monkeypatch, tmp_path):
    entries_path = _prepare(migrated_conn, monkeypatch, tmp_path)

    result = runner.invoke(
        app,
        ["add"],
        input=(
            "BUY\n2026-01-10\nDEMO-BROKER-CTO\nDEMO\n10\n100\n\n1\n0\n\n"
            "initial buy via wizard\ny\n"
        ),
    )
    assert result.exit_code == 0, result.output
    assert "Loaded: inserted 1" in result.output
    assert entries_path.exists()

    with migrated_conn.cursor() as cur:
        cur.execute(
            "SELECT txn_type, quantity, gross_amount, net_amount, currency "
            "FROM core.transactions WHERE account_id = 'DEMO-BROKER-CTO'"
        )
        txn_type, quantity, gross_amount, net_amount, currency = cur.fetchone()
    assert txn_type == "BUY"
    assert quantity == Decimal("10")
    assert gross_amount == Decimal("1000")
    assert net_amount == Decimal("-1001")
    assert currency == "EUR"


def test_add_dividend_end_to_end(migrated_conn, monkeypatch, tmp_path):
    _prepare(migrated_conn, monkeypatch, tmp_path)

    result = runner.invoke(
        app,
        ["add"],
        input="DIVIDEND\n2026-01-20\nDEMO-BROKER-CTO\nDEMO\n9.60\n0\n1.44\n\nQ1 dividend\ny\n",
    )
    assert result.exit_code == 0, result.output
    assert "Loaded: inserted 1" in result.output

    with migrated_conn.cursor() as cur:
        cur.execute(
            "SELECT txn_type, gross_amount, tax, net_amount "
            "FROM core.transactions WHERE txn_type = 'DIVIDEND'"
        )
        txn_type, gross_amount, tax, net_amount = cur.fetchone()
    assert txn_type == "DIVIDEND"
    assert gross_amount == Decimal("9.60")
    assert tax == Decimal("1.44")
    assert net_amount == Decimal("8.16")


def test_add_deposit_end_to_end(migrated_conn, monkeypatch, tmp_path):
    _prepare(migrated_conn, monkeypatch, tmp_path)

    result = runner.invoke(
        app,
        ["add"],
        input="DEPOSIT\n2026-02-01\nDEMO-BANK-CURRENT\n2000\n\nmonthly transfer\ny\n",
    )
    assert result.exit_code == 0, result.output
    assert "Loaded: inserted 1" in result.output

    with migrated_conn.cursor() as cur:
        cur.execute(
            "SELECT txn_type, gross_amount, net_amount, currency "
            "FROM core.transactions WHERE account_id = 'DEMO-BANK-CURRENT'"
        )
        txn_type, gross_amount, net_amount, currency = cur.fetchone()
    assert txn_type == "DEPOSIT"
    assert gross_amount == Decimal("2000")
    assert net_amount == Decimal("2000")
    assert currency == "EUR"


def test_add_cancel_writes_nothing(migrated_conn, monkeypatch, tmp_path):
    entries_path = _prepare(migrated_conn, monkeypatch, tmp_path)

    result = runner.invoke(
        app,
        ["add"],
        input="DEPOSIT\n2026-02-01\nDEMO-BANK-CURRENT\n2000\n\ncancel me\nn\n",
    )
    assert result.exit_code == 0, result.output
    assert "Cancelled, nothing written" in result.output
    assert not entries_path.exists()

    with migrated_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.transactions")
        assert cur.fetchone()[0] == 0


def test_add_symbol_typo_shows_suggestion_and_recovers(migrated_conn, monkeypatch, tmp_path):
    _prepare(migrated_conn, monkeypatch, tmp_path)

    result = runner.invoke(
        app,
        ["add"],
        input=(
            "BUY\n2026-01-10\nDEMO-BROKER-CTO\nDEM\nDEMO\n10\n100\n\n1\n0\n\n"
            "typo recovery\ny\n"
        ),
    )
    assert result.exit_code == 0, result.output
    assert "does not resolve" in result.output
    assert "DEMO" in result.output
    assert "Loaded: inserted 1" in result.output
