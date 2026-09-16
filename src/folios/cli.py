from datetime import date
from pathlib import Path

import typer

from folios import __version__, db, seed
from folios import fx as fx_module
from folios import loader as loader_module
from folios import validate as validate_module

app = typer.Typer(
    name="folios",
    help="A local, single-user portfolio tracker.",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """A local, single-user portfolio tracker."""


@app.command()
def version() -> None:
    """Print the installed folios version."""
    typer.echo(__version__)


@app.command()
def init() -> None:
    """Apply pending migrations, then seed reference data from config/."""
    conn = db.connect()
    try:
        applied = db.run_migrations(conn)
        if applied:
            for filename in applied:
                typer.echo(f"applied {filename}")
        else:
            typer.echo("migrations up to date, nothing to apply")

        try:
            result = seed.seed(conn)
        except seed.SeedValidationError as exc:
            for error in exc.errors:
                typer.echo(error, err=True)
            raise typer.Exit(code=1) from exc

        for line in result.summary_lines():
            typer.echo(line)
        for warning in result.warnings:
            typer.echo(f"warning: {warning}")
    finally:
        conn.close()


@app.command()
def fx(
    since: str = typer.Option(
        ...,
        "--since",
        help="ISO date (YYYY-MM-DD). Only used as a floor when no rates "
        "are stored yet — later runs resume from the last stored date.",
    ),
) -> None:
    """Fetch EUR-based FX rates for every currency used in config/."""
    since_date = date.fromisoformat(since)
    conn = db.connect()
    try:
        currencies = fx_module.currencies_from_config()
        if not currencies:
            typer.echo("no currencies found in config/, nothing to fetch")
            return

        start = fx_module.determine_fetch_start(conn, since_date)
        provider = fx_module.FrankfurterFxProvider()
        rates = provider.fetch_rates(start, currencies)
        inserted = fx_module.store_rates(conn, rates)
        typer.echo(
            f"stored {inserted} rate rows for "
            f"{', '.join(sorted(c for c in currencies if c != 'EUR'))}"
        )
    finally:
        conn.close()


@app.command()
def validate(
    path: str = typer.Argument(
        str(validate_module.DEFAULT_ENTRIES_PATH),
        help="Transactions CSV to check (defaults to data/manual/transactions.csv)",
    ),
) -> None:
    """Validate a transactions CSV. A dry run — never writes to the database."""
    conn = db.connect()
    try:
        messages = validate_module.validate_file(conn, Path(path))
    finally:
        conn.close()

    for message in messages:
        typer.echo(str(message))

    error_count = sum(1 for m in messages if m.level == "error")
    if error_count:
        typer.echo(f"{error_count} error(s)", err=True)
        raise typer.Exit(code=1)
    typer.echo("no errors")


@app.command()
def load(
    path: str = typer.Argument(
        str(validate_module.DEFAULT_ENTRIES_PATH),
        help="Transactions CSV to load (defaults to data/manual/transactions.csv)",
    ),
) -> None:
    """Load a transactions CSV. Refuses to insert anything if any row
    fails validation."""
    conn = db.connect()
    try:
        try:
            result = loader_module.load_file(conn, Path(path))
        except loader_module.LoadValidationError as exc:
            for error in exc.errors:
                typer.echo(error, err=True)
            raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    typer.echo(
        f"read {result.rows_read}, inserted {result.rows_inserted}, "
        f"updated {result.rows_updated}, skipped {result.rows_skipped}"
    )
    for entry_id, fields in result.updated_fields.items():
        typer.echo(f"  {entry_id}: changed {', '.join(fields)}")


@app.command()
def rebuild() -> None:
    """Reproduce core.transactions and reference data from config/ and
    data/manual/ alone. Never touches fx_rates or prices — those come
    from external APIs, not files."""
    conn = db.connect()
    try:
        result = loader_module.rebuild(conn)
    finally:
        conn.close()
    typer.echo(
        f"rebuilt: read {result.rows_read}, inserted {result.rows_inserted}, "
        f"updated {result.rows_updated}, skipped {result.rows_skipped}"
    )


if __name__ == "__main__":
    app()
