import typer

from folios import __version__

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


if __name__ == "__main__":
    app()
