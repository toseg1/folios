import psycopg
import pytest

from folios.db import applied_migrations, pending_migrations, run_migrations


def test_run_migrations_applies_all_pending_in_order(clean_test_db):
    applied = run_migrations(clean_test_db)
    assert applied == sorted(applied)  # filename order
    assert "001_schemas_and_core.sql" in applied
    assert pending_migrations(clean_test_db) == []


def test_run_migrations_is_idempotent(migrated_conn):
    assert run_migrations(migrated_conn) == []
    assert pending_migrations(migrated_conn) == []


def test_core_tables_exist(migrated_conn):
    with migrated_conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'core'
            """
        )
        tables = {row[0] for row in cur.fetchall()}

    assert {
        "schema_migrations",
        "dimensions",
        "accounts",
        "instruments",
        "instrument_fund",
        "instrument_bond",
        "instrument_crypto",
        "instrument_aliases",
        "etp_exposure",
        "transactions",
        "prices",
        "fx_rates",
        "statement_positions",
        "statement_cash",
        "load_runs",
    } <= tables


def test_marts_and_staging_schemas_exist(migrated_conn):
    with migrated_conn.cursor() as cur:
        cur.execute(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name IN ('marts', 'staging')"
        )
        schemas = {row[0] for row in cur.fetchall()}
    assert schemas == {"marts", "staging"}


def test_sign_convention_rejects_positive_buy_net_amount(migrated_conn):
    with migrated_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.accounts (account_id, broker, account_type, base_currency)
            VALUES ('TEST-ACC', 'Test Broker', 'securities', 'EUR')
            """
        )
        cur.execute(
            """
            INSERT INTO core.instruments (instrument_id, name, asset_class, currency)
            VALUES ('TEST-INSTR', 'Test Instrument', 'EQUITY', 'EUR')
            """
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                """
                INSERT INTO core.transactions
                    (entry_id, txn_hash, account_id, trade_date, txn_type,
                     instrument_id, quantity, price, net_amount, currency)
                VALUES
                    ('csv:test:1', 'hash1', 'TEST-ACC', '2026-01-01', 'BUY',
                     'TEST-INSTR', 10, 100, 1000, 'EUR')
                """
            )
    migrated_conn.rollback()


def test_applied_migrations_empty_before_any_run(clean_test_db):
    assert applied_migrations(clean_test_db) == set()
