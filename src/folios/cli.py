import typer

from folios import __version__, db

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
    """Apply any pending migrations to the folios database."""
    conn = db.connect()
    try:
        applied = db.run_migrations(conn)
    finally:
        conn.close()

    if applied:
        for filename in applied:
            typer.echo(f"applied {filename}")
    else:
        typer.echo("up to date, nothing to apply")


if __name__ == "__main__":
    app()
