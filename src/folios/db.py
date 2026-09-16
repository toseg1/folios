from __future__ import annotations

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

# src/folios/db.py -> src/folios -> src -> repo root. Under `pip install -e .`
# this still resolves correctly since editable installs point back at the
# source tree, not a copy — and folios is only ever run from a cloned repo.
REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


def get_database_url() -> str:
    """Resolve the folios database connection string.

    FOLIOS_DATABASE_URL overrides everything else. Otherwise, build a
    connection to the local Docker Postgres as the folios_loader role
    from POSTGRES_PORT / FOLIOS_LOADER_PASSWORD in .env.
    """
    load_dotenv(REPO_ROOT / ".env")

    url = os.environ.get("FOLIOS_DATABASE_URL")
    if url:
        return url

    password = os.environ.get("FOLIOS_LOADER_PASSWORD")
    if not password:
        raise RuntimeError(
            "Set FOLIOS_LOADER_PASSWORD (or FOLIOS_DATABASE_URL) in .env"
        )
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("POSTGRES_PORT", "5432")
    return f"postgresql://folios_loader:{password}@{host}:{port}/folios"


def connect(database_url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(database_url or get_database_url())


def applied_migrations(conn: psycopg.Connection) -> set[str]:
    """Filenames already recorded in core.schema_migrations.

    Returns an empty set if that table doesn't exist yet — true on a
    database that has never had 001_schemas_and_core.sql applied, since
    that migration is what creates the table in the first place.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'core' AND table_name = 'schema_migrations'
            )
            """
        )
        (table_exists,) = cur.fetchone()
        if not table_exists:
            return set()
        cur.execute("SELECT filename FROM core.schema_migrations")
        return {row[0] for row in cur.fetchall()}


def pending_migrations(
    conn: psycopg.Connection, migrations_dir: Path = MIGRATIONS_DIR
) -> list[Path]:
    applied = applied_migrations(conn)
    return sorted(p for p in migrations_dir.glob("*.sql") if p.name not in applied)


def run_migrations(
    conn: psycopg.Connection, migrations_dir: Path = MIGRATIONS_DIR
) -> list[str]:
    """Apply every pending migration, in filename order, one per transaction.

    Forward-only: nothing here edits or re-runs a filename already recorded
    in core.schema_migrations.
    """
    newly_applied: list[str] = []
    for path in pending_migrations(conn, migrations_dir):
        with conn.cursor() as cur:
            cur.execute(path.read_text())
            cur.execute(
                "INSERT INTO core.schema_migrations (filename) VALUES (%s)",
                (path.name,),
            )
        conn.commit()
        newly_applied.append(path.name)
    return newly_applied
