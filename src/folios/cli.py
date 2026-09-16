from datetime import date
from pathlib import Path

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.prompt import Confirm, Prompt

from folios import __version__, db, seed
from folios import add as add_module
from folios import fx as fx_module
from folios import loader as loader_module
from folios import prices as prices_module
from folios import status as status_module
from folios import validate as validate_module
from folios import valuations as valuations_module
from folios.models import EntryRow, compute_net_amount, effective_gross

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


@app.command()
def add() -> None:
    """Interactively record one transaction — a BUY, DIVIDEND, DEPOSIT
    and so on — without opening the CSV."""
    console = Console()
    conn = db.connect()
    try:
        accounts = sorted(validate_module.known_accounts(conn))
        aliases = validate_module.alias_to_instrument(conn)
        currencies_by_instrument = validate_module.instrument_currencies(conn)

        if not accounts:
            console.print("[red]No accounts found — run `folios init` first.[/red]")
            raise typer.Exit(code=1)

        txn_type = Prompt.ask("Type", choices=sorted(add_module.FIELDS_FOR_TYPE))
        fields = add_module.FIELDS_FOR_TYPE[txn_type]

        entry_date = Prompt.ask("Date (YYYY-MM-DD)", default=date.today().isoformat())
        account = Prompt.ask("Account", choices=accounts)

        symbol: str | None = None
        if fields.symbol is not None:
            optional = fields.symbol == "optional"
            while True:
                answer = Prompt.ask(
                    "Symbol" + (" (optional)" if optional else ""), default=""
                )
                if answer == "":
                    if optional:
                        break
                    console.print("[red]Symbol is required for this type.[/red]")
                    continue
                if answer in aliases:
                    symbol = answer
                    break
                suggestions = add_module.suggest_symbols(answer, sorted(aliases))
                hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
                console.print(f"[red]{answer!r} does not resolve.[/red]{hint}")

        quantity = Prompt.ask("Quantity") if fields.quantity else ""
        price = Prompt.ask("Price") if fields.price else ""

        default_currency = "EUR"
        if symbol is not None:
            default_currency = currencies_by_instrument.get(
                aliases[symbol], "EUR"
            )

        gross = ""
        if fields.gross:
            gross_prompt = (
                "Gross (blank to compute from quantity x price)"
                if fields.quantity and fields.price
                else "Gross"
            )
            gross = Prompt.ask(gross_prompt, default="")
        fee = Prompt.ask("Fee", default="0") if fields.fee else "0"
        tax = Prompt.ask("Tax", default="0") if fields.tax else "0"
        currency = Prompt.ask("Currency", default=default_currency)
        note = Prompt.ask("Note", default="")

        raw_row = {
            "date": entry_date,
            "account": account,
            "type": txn_type,
            "symbol": symbol or "",
            "quantity": quantity,
            "price": price,
            "gross": gross,
            "fee": fee,
            "tax": tax,
            "currency": currency,
            "note": note,
        }

        try:
            row = EntryRow(
                entry_date=raw_row["date"],
                account=raw_row["account"],
                type=raw_row["type"],
                symbol=raw_row["symbol"] or None,
                quantity=raw_row["quantity"] or None,
                price=raw_row["price"] or None,
                gross=raw_row["gross"] or None,
                fee=raw_row["fee"],
                tax=raw_row["tax"],
                currency=raw_row["currency"],
                note=raw_row["note"] or None,
            )
        except ValidationError as exc:
            for error in exc.errors():
                console.print(f"[red]{error['msg']}[/red]")
            raise typer.Exit(code=1) from exc

        net_amount = compute_net_amount(row)
        gross_effective = effective_gross(row)
        console.print(
            f"\n{row.type} on {row.entry_date} in {row.account}: "
            f"gross={gross_effective}, fee={row.fee}, tax={row.tax}, "
            f"currency={row.currency} -> net effect {net_amount} {row.currency}\n"
        )

        if not Confirm.ask("Record this transaction?"):
            console.print("Cancelled, nothing written.")
            return

        path = validate_module.DEFAULT_ENTRIES_PATH
        line_number, relative = add_module.append_entry(path, raw_row)
        console.print(f"Appended to {relative}:{line_number}")

        try:
            result = loader_module.load_file(conn, path)
        except loader_module.LoadValidationError as exc:
            for error in exc.errors:
                console.print(f"[red]{error}[/red]")
            raise typer.Exit(code=1) from exc

        console.print(
            f"Loaded: inserted {result.rows_inserted}, "
            f"updated {result.rows_updated}, skipped {result.rows_skipped}"
        )
    finally:
        conn.close()


@app.command()
def prices() -> None:
    """Fetch closing prices for every yfinance-priced instrument ever
    held, gap-filling from each instrument's last stored date.
    price_source=manual instruments are skipped, not reported."""
    conn = db.connect()
    try:
        stored, warnings = prices_module.refresh_prices(conn)
    finally:
        conn.close()

    for warning in warnings:
        typer.echo(f"warning: {warning}")
    typer.echo(f"stored {stored} price rows")


@app.command()
def value(
    path: str = typer.Argument(
        str(valuations_module.DEFAULT_VALUATIONS_PATH),
        help="Valuations CSV to load (defaults to data/manual/valuations.csv)",
    ),
) -> None:
    """Load manual valuations (SCPI, unites de compte, unlisted funds —
    anything with price_source=manual) into core.prices. Refuses to
    write anything if any row fails validation."""
    conn = db.connect()
    try:
        try:
            result = valuations_module.load_valuations(conn, Path(path))
        except valuations_module.ValuationError as exc:
            for error in exc.errors:
                typer.echo(error, err=True)
            raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    typer.echo(f"read {result.rows_read}, stored {result.rows_stored}")


@app.command()
def status() -> None:
    """Flags things worth a human's attention: stale manual valuations,
    and more as later steps add checks here."""
    conn = db.connect()
    try:
        findings = status_module.run_status_checks(conn)
    finally:
        conn.close()

    if not findings:
        typer.echo("nothing to flag")
        return
    for finding in findings:
        typer.echo(str(finding))


if __name__ == "__main__":
    app()
