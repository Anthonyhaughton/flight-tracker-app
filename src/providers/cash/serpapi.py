"""SerpApi Google Flights wrapper -- the default CashFareProvider (see
.claude/skills/flight-cash-price-monitor). There is no official Google
Flights API; this is a paid scraping-API wrapper that returns clean JSON.

Every search is explicitly ONE-WAY (`type=2`). seats.aero's award costs are
one-way (a Cached Search hit is priced for a single direction), so a
round-trip cash comparison would roughly double the true one-way price,
silently inflating every effective-CPP number in valuation.py without any
test ever failing -- the bug wouldn't look "wrong" in isolation, it would
just make every redemption look worse than it really is. `_search_one`
requests `type="2"` and never sends `return_date`, and `_parse_itinerary`
additionally refuses to parse any itinerary whose own `type` field (SerpApi
echoes it per-itinerary) isn't `"One way"`, as a second, independent guard
against this exact failure mode -- see test_serpapi.py's explicit
one-way-vs-round-trip assertions.

Schema confirmed against SerpApi's live Google Flights API reference and
its general status/error-code reference (2026-07) -- see this module's
`_get` for the exact status codes and error body shape.
"""

from __future__ import annotations

import datetime
import logging

import httpx

from src.providers.cash.base import CashFare

logger = logging.getLogger(__name__)

BASE_URL = "https://serpapi.com/search"

# travel_class: "1" economy (default), "2" premium economy, "3" business,
# "4" first -- per SerpApi's Google Flights API reference.
_CABIN_TO_TRAVEL_CLASS = {
    "economy": "1",
    "premium_economy": "2",
    "business": "3",
    "first": "4",
}

_ONE_WAY_TYPE_LABEL = "One way"


class SerpApiError(RuntimeError):
    pass


class SerpApiAuthError(SerpApiError):
    pass


class SerpApiRateLimitError(SerpApiError):
    pass


class SerpApiTimeoutError(SerpApiError):
    """A transient network-level timeout (httpx.TimeoutException), distinct
    from auth/quota failures -- the request may or may not have completed
    server-side (we never got a response either way), so callers should
    treat this as "unknown, safe to skip and retry later," not as a sign
    the key/quota is broken. Queries far out on the calendar (sparse/slow
    upstream data, e.g. Google Flights ~1 year ahead) appear more likely to
    hit this than near-term ones -- confirmed via a real
    scripts/dry_run.py run against a 2027 date."""


class SerpApiClient:
    """Implements the CashFareProvider protocol (src/providers/cash/base.py)."""

    def __init__(self, api_key: str, *, client: httpx.Client | None = None, max_retries: int = 1):
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=20.0)
        self._max_retries = max_retries

    def close(self) -> None:
        self._client.close()

    def search(
        self,
        origin: str,
        destinations: list[str],
        start: datetime.date,
        end: datetime.date,
        cabin: str,
    ) -> list[CashFare]:
        """One CashFare per destination that has results -- the cheapest
        one-way itinerary SerpApi finds for `start` specifically.

        Unlike seats.aero's Cached Search, Google Flights has no
        date-range/calendar mode on this engine: each request prices ONE
        concrete outbound_date. `start` is used as that date and `end` is
        ignored -- callers (src/cash.py) are expected to call this once per
        route/cabin/date-bucket on a caching cadence (cash_baseline_minutes
        in watchlist.yaml), not once per exact day, which is what actually
        bounds call volume/cost. See src/cash.py's baseline_key bucketing.
        """
        fares: list[CashFare] = []
        for destination in destinations:
            fare = self._search_one(origin, destination, start, cabin)
            if fare is not None:
                fares.append(fare)
        return fares

    def _search_one(self, origin: str, destination: str, date: datetime.date, cabin: str) -> CashFare | None:
        travel_class = _CABIN_TO_TRAVEL_CLASS.get(cabin)
        if travel_class is None:
            raise ValueError(f"unknown cabin {cabin!r}, expected one of {sorted(_CABIN_TO_TRAVEL_CLASS)}")

        params = {
            "engine": "google_flights",
            "api_key": self._api_key,
            "departure_id": origin,
            "arrival_id": destination,
            "outbound_date": date.isoformat(),
            "type": "2",  # one-way -- MUST match seats.aero's one-way award pricing, see module docstring
            "travel_class": travel_class,
            "currency": "USD",
            "hl": "en",
            "gl": "us",
        }
        data = self._get(params)

        status = (data.get("search_metadata") or {}).get("status")
        if status == "Error":
            logger.warning(
                "SerpApi query error for %s->%s (%s) on %s: %s",
                origin, destination, cabin, date, data.get("error"),
            )
            return None

        candidates = (data.get("best_flights") or []) + (data.get("other_flights") or [])
        one_way_candidates = [c for c in candidates if c.get("type", _ONE_WAY_TYPE_LABEL) == _ONE_WAY_TYPE_LABEL]
        skipped = len(candidates) - len(one_way_candidates)
        if skipped:
            logger.warning(
                "SerpApi returned %d non-one-way itinerar(y/ies) for a type=2 request "
                "(%s->%s on %s) -- discarding them rather than risk an inflated CPP comparison",
                skipped, origin, destination, date,
            )
        if not one_way_candidates:
            return None

        cheapest = min(one_way_candidates, key=lambda c: c["price"])
        return _parse_itinerary(cheapest, origin, destination, date, cabin)

    def _get(self, params: dict) -> dict:
        try:
            response = self._client.get(BASE_URL, params=params)
        except httpx.TimeoutException as exc:
            raise SerpApiTimeoutError(f"SerpApi request timed out ({type(exc).__name__}): {exc}") from exc

        if response.status_code == 401:
            raise SerpApiAuthError(
                f"SerpApi rejected the API key (401): {_error_message(response)}. Check SERPAPI_KEY."
            )
        if response.status_code == 429:
            raise SerpApiRateLimitError(f"SerpApi quota/rate limit exhausted (429): {_error_message(response)}")
        response.raise_for_status()
        return response.json()


def _error_message(response: httpx.Response) -> str:
    try:
        return response.json().get("error", response.text)
    except ValueError:
        return response.text


def _parse_itinerary(itinerary: dict, origin: str, destination: str, date: datetime.date, cabin: str) -> CashFare:
    legs = itinerary["flights"]
    first_leg = legs[0]
    return CashFare(
        origin=origin,
        destination=destination,
        date=date,
        return_date=None,  # one-way search -- never populated, see module docstring
        cabin=cabin,
        price_usd=float(itinerary["price"]),
        airline=first_leg["airline"],
        stops=len(legs) - 1,
        # booking_token resolves to a real URL only via a second, separately
        # billed SerpApi request -- not fetched here to keep provider cost
        # down (see flight-cash-price-monitor's "cache aggressively").
        deep_link=None,
    )
