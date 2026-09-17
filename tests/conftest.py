import os

import psycopg
import pytest

from folios.db import run_migrations

TEST_DATABASE_URL = os.environ.get(
    "FOLIOS_TEST_DATABASE_URL",
    "postgresql://folios_test:folios_test@localhost:5432/folios_test",
)


@pytest.fixture()
def clean_test_db():
    """Connection to a disposable database, wiped of core/marts/staging and
    with no migrations applied. Never used against the real `folios` DB —
    only the dedicated folios_test one (see FOLIOS_TEST_DATABASE_URL)."""
    conn = psycopg.connect(TEST_DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS core CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS marts CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS staging CASCADE")
        # docker/initdb/01-roles.sql creates this in the real stack;
        # migrations/003_metabase_access.sql assumes it already exists.
        # The role is cluster-level (DROP SCHEMA above doesn't touch it),
        # so this only actually runs once per disposable cluster's life.
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'metabase_ro'")
        if cur.fetchone() is None:
            cur.execute("CREATE ROLE metabase_ro LOGIN PASSWORD 'test'")
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def migrated_conn(clean_test_db):
    """Same disposable database, with every migration freshly applied."""
    run_migrations(clean_test_db)
    return clean_test_db
