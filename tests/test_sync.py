import shutil
from datetime import date
from unittest.mock import MagicMock

import pytest

from folios import fx as fx_module
from folios import loader as loader_module
from folios import prices as prices_module
from folios import seed as seed_module
from folios import sync
from folios import valuations as valuations_module
from folios.google import forms as google_forms
from folios.google import sheets as google_sheets
from folios.google.sheets import PullResult
from folios.seed import seed
from tests.test_seed import EXAMPLE_CONFIG


@pytest.fixture()
def isolated_sync_env(tmp_path, monkeypatch):
    config_copy = tmp_path / "config"
    shutil.copytree(EXAMPLE_CONFIG, config_copy)
    monkeypatch.setattr(seed_module, "CONFIG_DIR", config_copy)

    manual_dir = tmp_path / "manual"
    manual_dir.mkdir()
    monkeypatch.setattr(loader_module, "DEFAULT_DATA_DIR", manual_dir)
    monkeypatch.setattr(
        valuations_module, "DEFAULT_VALUATIONS_PATH", manual_dir / "valuations.csv"
    )

    monkeypatch.setattr(sync, "LOG_PATH", tmp_path / "logs" / "sync.log")

    return {"config_dir": config_copy, "manual_dir": manual_dir}


@pytest.fixture()
def seeded_conn(migrated_conn, isolated_sync_env):
    seed(migrated_conn, isolated_sync_env["config_dir"])
    return migrated_conn


def _stub_fx(monkeypatch, rates=None):
    fake_provider = MagicMock()
    fake_provider.fetch_rates.return_value = rates or {}
    monkeypatch.setattr(fx_module, "FrankfurterFxProvider", lambda: fake_provider)
    return fake_provider


def _stub_prices(monkeypatch, stored=0, warnings=None):
    monkeypatch.setattr(
        prices_module, "refresh_prices", MagicMock(return_value=(stored, warnings or []))
    )


def _stub_clean_pull(monkeypatch, **kwargs):
    monkeypatch.setattr(google_sheets, "pull", MagicMock(return_value=PullResult(**kwargs)))


def test_run_sync_skips_pull_gracefully_when_form_not_initialized(seeded_conn, monkeypatch):
    def raise_not_initialized(conn):
        raise google_forms.FormNotInitializedError

    monkeypatch.setattr(google_sheets, "pull", raise_not_initialized)
    _stub_fx(monkeypatch)
    _stub_prices(monkeypatch)

    result = sync.run_sync(seeded_conn)

    pull_step = next(s for s in result.steps if s.name == "pull")
    assert pull_step.ok is True
    assert "form-init" in pull_step.summary
    assert result.ok is True


def test_run_sync_runs_every_step_in_order_and_is_ok_when_nothing_fails(
    seeded_conn, monkeypatch
):
    _stub_clean_pull(monkeypatch)
    _stub_fx(monkeypatch)
    _stub_prices(monkeypatch, stored=3)

    result = sync.run_sync(seeded_conn)

    assert [s.name for s in result.steps] == ["pull", "fx", "load", "value", "prices", "status"]
    assert result.ok is True


def test_run_sync_continues_past_a_pull_exception(seeded_conn, monkeypatch):
    monkeypatch.setattr(
        google_sheets, "pull", MagicMock(side_effect=RuntimeError("Google API down"))
    )
    _stub_fx(monkeypatch)
    _stub_prices(monkeypatch)

    result = sync.run_sync(seeded_conn)

    pull_step = next(s for s in result.steps if s.name == "pull")
    assert pull_step.ok is False
    assert "Google API down" in pull_step.details[0]
    # Every later step still ran despite pull failing.
    assert [s.name for s in result.steps] == ["pull", "fx", "load", "value", "prices", "status"]
    assert result.ok is False


def test_run_sync_reports_pull_response_errors_as_a_failed_step(seeded_conn, monkeypatch):
    _stub_clean_pull(monkeypatch, responses_seen=2, errors=["r1: account=BAD does not exist"])
    _stub_fx(monkeypatch)
    _stub_prices(monkeypatch)

    result = sync.run_sync(seeded_conn)

    pull_step = next(s for s in result.steps if s.name == "pull")
    assert pull_step.ok is False
    assert result.ok is False


def test_run_sync_continues_past_a_broken_transaction_file(
    seeded_conn, monkeypatch, isolated_sync_env
):
    (isolated_sync_env["manual_dir"] / "gform_2026-01-15.csv").write_text(
        "date,account,type,symbol,quantity,price,gross,fee,tax,currency,note\n"
        "2026-01-15,NOT-A-REAL-ACCOUNT,DEPOSIT,,,,500,0,0,EUR,bad row\n"
    )
    _stub_clean_pull(monkeypatch)
    _stub_fx(monkeypatch)
    _stub_prices(monkeypatch)

    result = sync.run_sync(seeded_conn)

    load_step = next(s for s in result.steps if s.name == "load")
    assert load_step.ok is False
    assert any("NOT-A-REAL-ACCOUNT" in d for d in load_step.details)
    # prices and status still ran after a failed load.
    assert [s.name for s in result.steps] == ["pull", "fx", "load", "value", "prices", "status"]
    assert result.ok is False


def test_run_sync_status_findings_never_fail_the_run(seeded_conn, monkeypatch):
    # DEMO-FUND-BOND / DEMO-BOND-2030 have no manual valuation yet in a
    # freshly seeded DB, so check_stale_manual_valuations always finds
    # something here — that must not flip status.ok or result.ok.
    _stub_clean_pull(monkeypatch)
    _stub_fx(monkeypatch)
    _stub_prices(monkeypatch)

    result = sync.run_sync(seeded_conn)

    status_step = next(s for s in result.steps if s.name == "status")
    assert status_step.ok is True
    assert len(status_step.details) > 0
    assert result.ok is True


def test_run_sync_is_idempotent_on_repeat_runs(seeded_conn, monkeypatch, isolated_sync_env):
    (isolated_sync_env["manual_dir"] / "transactions.csv").write_text(
        "date,account,type,symbol,quantity,price,gross,fee,tax,currency,note\n"
        "2026-01-05,DEMO-BROKER-CTO,BUY,DEMO,10,100,1000,1,0,EUR,\n"
    )
    _stub_clean_pull(monkeypatch)
    _stub_fx(monkeypatch)
    _stub_prices(monkeypatch)

    first = sync.run_sync(seeded_conn)
    second = sync.run_sync(seeded_conn)

    assert first.ok is True
    assert second.ok is True
    load_step_1 = next(s for s in first.steps if s.name == "load")
    load_step_2 = next(s for s in second.steps if s.name == "load")
    assert "inserted 1" in load_step_1.summary
    assert "inserted 0" in load_step_2.summary


def test_default_fx_since_uses_earliest_trade_date(seeded_conn):
    with seeded_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions
                (entry_id, txn_hash, account_id, trade_date, txn_type,
                 instrument_id, quantity, price, gross_amount, fee, tax,
                 net_amount, currency, fx_rate_to_base, fx_rate_date, source_file)
            VALUES
                ('csv:test:1', 'h', 'DEMO-BROKER-CTO', '2025-06-01', 'BUY',
                 'DEMO-SHARE', 1, 100, 100, 0, 0, -100, 'EUR', 1, '2025-06-01', 'test')
            """
        )
    seeded_conn.commit()

    assert sync._default_fx_since(seeded_conn) == date(2025, 6, 1)


def test_default_fx_since_falls_back_to_today_with_no_transactions(seeded_conn):
    assert sync._default_fx_since(seeded_conn) == date.today()


def test_append_log_writes_readable_summary(seeded_conn, monkeypatch, isolated_sync_env):
    _stub_clean_pull(monkeypatch)
    _stub_fx(monkeypatch)
    _stub_prices(monkeypatch, stored=2)

    result = sync.run_sync(seeded_conn)
    log_path = sync.append_log(result)

    content = log_path.read_text()
    assert "=== folios sync" in content
    assert "[ok] pull:" in content
    assert "[ok] fx:" in content
    assert "[ok] load:" in content
    assert "[ok] value:" in content
    assert "[ok] prices: stored 2 price row(s)" in content
    assert "=== ok ===" in content

    # A second run appends rather than overwriting.
    sync.append_log(sync.run_sync(seeded_conn), log_path=log_path)
    assert content in log_path.read_text()
    assert log_path.read_text().count("=== folios sync") == 2
