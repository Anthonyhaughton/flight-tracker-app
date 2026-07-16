"""CashFareProvider interface -- the swap point for cash fare sourcing.

This is the contract only. The v1.1 implementation (SerpApi Google Flights,
see .claude/skills/flight-cash-price-monitor) is not built yet; v1.0 is
award-only and never imports this module from the poller.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CashFare:
    origin: str
    destination: str
    date: datetime.date
    return_date: datetime.date | None
    cabin: str
    price_usd: float
    airline: str
    stops: int
    deep_link: str | None


class CashFareProvider(Protocol):
    def search(
        self,
        origin: str,
        destinations: list[str],
        start: datetime.date,
        end: datetime.date,
        cabin: str,
    ) -> list[CashFare]: ...
