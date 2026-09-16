from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Protocol


class FxProvider(Protocol):
    def fetch_rates(
        self, since: date, currencies: set[str]
    ) -> dict[date, dict[str, Decimal]]:
        """EUR-based daily rates for `currencies`, from `since` through the
        most recent available date. Returns {date: {quote_ccy: rate}} —
        only dates that actually have published rates (weekdays)."""
        ...
