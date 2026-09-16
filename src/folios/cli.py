import typer

from folios import __version__, db, seed

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


if __name__ == "__main__":
    app()
