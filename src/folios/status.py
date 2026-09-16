from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import psycopg

from folios import valuations

# `folios status` accumulates checks across several build-plan steps
# (8b: stale valuations; 8c: unmapped exposures; 16: missing tickers).
# This module is the aggregation point — each check is its own function,
# run_status_checks() is what the CLI calls.
DEFAULT_STALE_AFTER_DAYS = 100


@dataclass
class Finding:
    text: str

    def __str__(self) -> str:
        return self.text


def check_stale_manual_valuations(
    conn: psycopg.Connection, max_age_days: int = DEFAULT_STALE_AFTER_DAYS
) -> list[Finding]:
    findings: list[Finding] = []
    today = date.today()

    for row in valuations.manual_priced_instruments(conn):
        last_valued = row["last_valued"]
        if last_valued is None:
            findings.append(
                Finding(
                    f"{row['instrument_id']} ({row['name']}): no manual "
                    f"valuation yet — run `folios value`"
                )
            )
            continue
        age_days = (today - last_valued).days
        if age_days > max_age_days:
            findings.append(
                Finding(
                    f"{row['instrument_id']} ({row['name']}): last valued "
                    f"{last_valued.isoformat()} ({age_days} days ago)"
                )
            )

    return findings


def run_status_checks(conn: psycopg.Connection) -> list[Finding]:
    return check_stale_manual_valuations(conn)
