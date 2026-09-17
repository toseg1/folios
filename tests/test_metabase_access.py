import psycopg
import pytest

from folios.loader import load_file
from folios.seed import seed
from tests.test_marts import GOOD_FIXTURE
from tests.test_seed import EXAMPLE_CONFIG


@pytest.fixture()
def metabase_ro_conn(migrated_conn):
    """migrated_conn, with a helper connection state for running queries
    as metabase_ro via SET ROLE — the standard way to test grants without
    a second physical connection. conftest.py's clean_test_db already
    ensures the role exists (docker's initdb script does this in the
    real stack)."""
    yield migrated_conn
    migrated_conn.rollback()
    with migrated_conn.cursor() as cur:
        cur.execute("RESET ROLE")


def test_metabase_ro_can_select_from_mart_views(metabase_ro_conn):
    with metabase_ro_conn.cursor() as cur:
        cur.execute("SET ROLE metabase_ro")
        cur.execute("SELECT count(*) FROM marts.v_positions")
        cur.execute("SELECT count(*) FROM marts.v_transactions")
        cur.execute("SELECT count(*) FROM marts.v_allocation")


def test_metabase_ro_cannot_select_from_core_tables(metabase_ro_conn):
    with metabase_ro_conn.cursor() as cur:
        cur.execute("SET ROLE metabase_ro")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("SELECT * FROM core.transactions")
    # A failed statement aborts the transaction; roll back before
    # anything else (including the fixture's own RESET ROLE) can run.
    metabase_ro_conn.rollback()


def test_metabase_ro_cannot_use_schema_core_at_all(metabase_ro_conn):
    # Not just table-level: USAGE on the schema itself was never granted,
    # so core is invisible before SELECT privilege even comes into it.
    with metabase_ro_conn.cursor() as cur:
        cur.execute("SET ROLE metabase_ro")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("SELECT * FROM core.accounts")
    metabase_ro_conn.rollback()


def test_default_privileges_cover_a_view_added_after_this_migration(metabase_ro_conn):
    # Proves ALTER DEFAULT PRIVILEGES actually works, not just the
    # explicit GRANT on views that already existed when 003 ran.
    with metabase_ro_conn.cursor() as cur:
        cur.execute("CREATE VIEW marts.v_test_future AS SELECT 1 AS one")
    metabase_ro_conn.commit()

    with metabase_ro_conn.cursor() as cur:
        cur.execute("SET ROLE metabase_ro")
        cur.execute("SELECT * FROM marts.v_test_future")
        assert cur.fetchone() == (1,)
        cur.execute("RESET ROLE")
        cur.execute("DROP VIEW marts.v_test_future")
    metabase_ro_conn.commit()


def test_marts_views_return_rows_for_metabase_ro(metabase_ro_conn):
    seed(metabase_ro_conn, EXAMPLE_CONFIG)
    load_file(metabase_ro_conn, GOOD_FIXTURE)

    with metabase_ro_conn.cursor() as cur:
        cur.execute("SET ROLE metabase_ro")
        cur.execute("SELECT count(*) FROM marts.v_transactions")
        assert cur.fetchone()[0] == 5
