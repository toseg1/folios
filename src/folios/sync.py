from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg

from folios import fx, loader, prices, status, valuations
from folios.google import forms as google_forms
from folios.google import sheets as google_sheets
from folios.validate import REPO_ROOT

LOG_PATH = REPO_ROOT / "logs" / "sync.log"


@dataclass
class StepResult:
    name: str
    ok: bool
    summary: str
    details: list[str] = field(default_factory=list)


@dataclass
class SyncResult:
    steps: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)


def _default_fx_since(conn: psycopg.Connection) -> date:
    """Only used as a floor when no FX rates are stored yet at all — see
    fx.determine_fetch_start, which resumes from the last stored date on
    every later run. The earliest trade date is the natural floor; with
    no transactions yet there is nothing to backfill."""
    with conn.cursor() as cur:
        cur.execute("SELECT MIN(trade_date) FROM core.transactions")
        (earliest,) = cur.fetchone()
    return earliest or date.today()


def _run_pull(conn: psycopg.Connection) -> StepResult:
    try:
        result = google_sheets.pull(conn)
    except google_forms.FormNotInitializedError:
        return StepResult("pull", True, "skipped — folios form-init has not been run yet")
    except Exception as exc:  # noqa: BLE001 - a network/API failure here must not crash sync
        return StepResult("pull", False, "failed", details=[str(exc)])

    summary = (
        f"{result.responses_seen} new response(s), "
        f"{result.transactions_written} transaction(s), "
        f"{result.valuations_written} valuation(s), "
        f"{len(result.new_instruments)} new instrument(s)"
    )
    return StepResult("pull", not result.errors, summary, details=list(result.errors))


def _run_fx(conn: psycopg.Connection) -> StepResult:
    try:
        currencies = fx.currencies_from_config()
        if not currencies:
            return StepResult("fx", True, "no currencies configured, nothing to fetch")
        since = fx.determine_fetch_start(conn, _default_fx_since(conn))
        provider = fx.FrankfurterFxProvider()
        rates = provider.fetch_rates(since, currencies)
        inserted = fx.store_rates(conn, rates)
        return StepResult("fx", True, f"stored {inserted} rate row(s)")
    except Exception as exc:  # noqa: BLE001 - fx failing must not crash sync
        return StepResult("fx", False, "failed", details=[str(exc)])


def _run_load(conn: psycopg.Connection) -> StepResult:
    result, errors = loader.load_all(conn)
    summary = (
        f"read {result.rows_read}, inserted {result.rows_inserted}, "
        f"updated {result.rows_updated}, skipped {result.rows_skipped}"
    )
    return StepResult("load", not errors, summary, details=errors)


def _run_value(conn: psycopg.Connection) -> StepResult:
    result, errors = valuations.load_all(conn)
    summary = f"read {result.rows_read}, stored {result.rows_stored}"
    return StepResult("value", not errors, summary, details=errors)


def _run_prices(conn: psycopg.Connection) -> StepResult:
    try:
        stored, warnings = prices.refresh_prices(conn)
        return StepResult("prices", True, f"stored {stored} price row(s)", details=list(warnings))
    except Exception as exc:  # noqa: BLE001 - yfinance failing must not crash sync
        return StepResult("prices", False, "failed", details=[str(exc)])


def _run_status(conn: psycopg.Connection) -> StepResult:
    findings = status.run_status_checks(conn)
    # Informational only — a stale valuation or unmapped exposure code
    # doesn't mean sync itself failed, so this step is always ok.
    return StepResult(
        "status", True, f"{len(findings)} finding(s)", details=[str(f) for f in findings]
    )


def run_sync(conn: psycopg.Connection) -> SyncResult:
    """folios sync = pull -> fx -> load -> value -> prices -> status.
    The order is a dependency chain, not a preference: fx must precede
    load (the loader freezes the rate onto each row); prices must follow
    load (it only fetches for instruments now held).

    Every step always runs — one broken phone submission shouldn't also
    block today's market-data refresh — but the run as a whole reports
    (and the CLI exits) non-zero if any step reported an error.
    Idempotent end to end: every write below is already an upsert, or
    (pull) tracks what has already been processed."""
    result = SyncResult()
    for step_fn in (_run_pull, _run_fx, _run_load, _run_value, _run_prices, _run_status):
        result.steps.append(step_fn(conn))
    return result


def format_log_entry(result: SyncResult) -> str:
    lines = [f"=== folios sync {datetime.now(UTC).isoformat(timespec='seconds')} ==="]
    for step in result.steps:
        word = "ok" if step.ok else "ERROR"
        lines.append(f"[{word}] {step.name}: {step.summary}")
        lines.extend(f"    {detail}" for detail in step.details)
    lines.append(f"=== {'ok' if result.ok else 'FAILED'} ===")
    return "\n".join(lines) + "\n"


def append_log(result: SyncResult, log_path: Path | None = None) -> Path:
    log_path = log_path if log_path is not None else LOG_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(format_log_entry(result))
    return log_path
