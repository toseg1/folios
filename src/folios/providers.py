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


class PriceProvider(Protocol):
    def fetch_prices(
        self, tickers: dict[str, str], since: date
    ) -> dict[str, dict[date, tuple[Decimal, Decimal | None]]]:
        """tickers: {instrument_id: yf_symbol}. Returns
        {instrument_id: {date: (close, adj_close)}}, from `since` through
        the most recent available trading day. An instrument whose ticker
        can't be resolved is simply absent from the result, never raises."""
        ...
