"""seats.aero Pro Partner API client — the only source of award availability.

We never scrape award space ourselves; seats.aero already normalizes it
across alliances and programs (see .claude/skills/seats-aero-integration).

Live Search is commercial-partner-only and unavailable on a Pro account, so
there is no live-confirm step here. Get Trips is the Pro-accessible
substitute: called right before alerting on a promising Cached Search hit,
it returns richer per-trip detail (real flight numbers, typed MileageCost/
TotalTaxes/Cabin/RemainingSeats) as a freshness/detail check, not a live
re-query of the airline. Cached Search already includes per-cabin taxes
too (confirmed against a real live call, 2026-07) -- Get Trips' figure is
just the more authoritative one when the two differ.
"""

from __future__ import annotations

import datetime
import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://seats.aero/partnerapi"

# Cabin-code prefix convention (Y=economy, W=premium economy, J=business,
# F=first) used on every per-cabin field in the Cached Search response.
_CABIN_CODES = {"economy": "Y", "premium_economy": "W", "business": "J", "first": "F"}

# Source (program) slugs seats.aero's own Concepts doc documents as not
# providing taxes/surcharges at all. Confirmed empirically against a real
# Cached Search call (2026-07): for these sources, {X}TotalTaxes is present
# on the wire but always 0 -- identical to what a genuine $0 co-pay would
# look like. There is no way to tell "unknown" from "really free" by
# inspecting the value alone, so these programs' taxes are always treated as
# unknown (None), never trusted at face value.
_PROGRAMS_WITHOUT_TAXES = {"qatar", "turkish", "singapore"}


class SeatsAeroError(RuntimeError):
    pass


class SeatsAeroAuthError(SeatsAeroError):
    pass


class SeatsAeroRateLimitError(SeatsAeroError):
    pass


@dataclass(frozen=True)
class AwardAvailability:
    origin: str
    destination: str
    date: datetime.date
    program: str            # the "Source" field
    cabin: str               # normalized from Y/W/J/F
    miles: int                # parsed from the {X}MileageCost string
    taxes_usd: float | None   # parsed from the {X}TotalTaxes int (cents); None if unknown
    airlines: list[str]       # parsed from the {X}Airlines comma-string
    direct: bool
    seats: int | None
    availability_id: str      # the "ID" field, used for Get Trips + dedup


def _cents_to_usd(cents) -> float:
    """The one place the cents->dollars division happens. Per seats.aero's
    own Concepts doc worked example, 70,000 miles + $12.90 taxes is
    represented as MileageCost: 70000, TotalTaxes: 1290."""
    return float(cents) / 100


def parse_trip_taxes_usd(trip: dict) -> float:
    """TotalTaxes on a Get Trips response is in cents -- see _cents_to_usd.
    Both the Telegram formatter and the poller's post-Get-Trips valuation
    recheck call this rather than duplicating the /100."""
    return _cents_to_usd(trip.get("TotalTaxes", 0))


def select_trip_for_cabin(trips: list[dict], cabin: str) -> dict | None:
    """Get Trips returns every itinerary for the AvailabilityID across ALL
    cabins on that route+date+program -- confirmed against a real call: one
    business-cabin Cached Search hit's Get Trips response held 88 trips
    spanning economy/premium/business/first, not just the cabin that
    matched. `trips[0]` is NOT guaranteed to be -- and in that real case
    wasn't -- the cabin we're alerting on.

    Picks the cheapest trip whose `Cabin` matches, or None if none does
    (the poller treats that like "no trip detail" -- skip rather than alert
    with the wrong cabin's miles/taxes)."""
    matching = [t for t in trips if t.get("Cabin") == cabin]
    if not matching:
        return None
    return min(matching, key=lambda t: t.get("MileageCost", float("inf")))


class SeatsAeroClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        client: httpx.Client | None = None,
        max_retries: int = 1,
    ):
        self._api_key = api_key
        self._base_url = base_url
        self._client = client or httpx.Client(timeout=15.0)
        self._max_retries = max_retries
        # Populated after every request -- the 1,000/day quota has no
        # per-minute signal otherwise, so this is the only visibility into
        # how much of the daily budget is left.
        self.last_rate_limit_remaining: str | None = None

    def cached_search(
        self,
        origin: str,
        destinations: list[str],
        start: datetime.date,
        end: datetime.date,
        cabins: list[str],
    ) -> list[AwardAvailability]:
        """Cheap, cached bulk availability -- poll this on every scheduled run.

        Deliberately omits `include_filtered`: leaving it false/omitted
        returns dynamic-price-filtered (saver-equivalent) results, which is
        what v1.0 wants. There is no per-item saver flag to check -- the
        filtering happens here, at request time.

        The request-side cabin filter is `cabins` (plural), a comma-list of
        full words like "economy,business" -- distinct from the single-letter
        Y/W/J/F prefixes the *response* uses on each field (see _parse_item).
        Confirmed against https://developers.seats.aero/reference/cached-search.md.
        """
        results: list[AwardAvailability] = []
        for destination in destinations:
            payload = self._get(
                "/search",
                {
                    "origin_airport": origin,
                    "destination_airport": destination,
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "cabins": ",".join(cabins),
                },
            )
            for item in payload.get("data", []):
                results.extend(self._parse_item(item, cabins))
        return results

    def get_trips(self, availability_id: str) -> list[dict] | None:
        """Per-trip detail on a Cached Search hit -- real flight numbers,
        typed MileageCost/TotalTaxes/Cabin/RemainingSeats. Call this only for
        a candidate that already cleared the valuation gate, right before
        alerting; it's an extra call against the same daily quota."""
        payload = self._get(f"/trips/{availability_id}", {})
        trips = payload.get("data")
        if not trips:
            return None
        return trips

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        return {"Partner-Authorization": self._api_key, "Accept": "application/json"}

    def _get(self, path: str, params: dict) -> dict:
        url = f"{self._base_url}{path}"
        attempt = 0
        while True:
            response = self._client.get(url, params=params, headers=self._headers())

            self.last_rate_limit_remaining = response.headers.get("X-RateLimit-Remaining")
            if self.last_rate_limit_remaining is not None:
                logger.info("seats.aero X-RateLimit-Remaining: %s", self.last_rate_limit_remaining)

            if response.status_code in (401, 403):
                raise SeatsAeroAuthError(
                    f"seats.aero returned {response.status_code}. Confirm your account has an "
                    "eligible Pro plan with Partner API access enabled (https://seats.aero)."
                )
            if response.status_code == 429:
                if attempt >= self._max_retries:
                    raise SeatsAeroRateLimitError(
                        "seats.aero daily quota (1,000 calls) is exhausted (429); "
                        "there's no reset until midnight UTC, so stop rather than retry."
                    )
                time.sleep(2**attempt)
                attempt += 1
                continue
            response.raise_for_status()
            return response.json()

    def _parse_item(self, item: dict, cabins: list[str]) -> list[AwardAvailability]:
        route = item["Route"]
        date = datetime.date.fromisoformat(item["Date"])
        program = item["Source"]
        out: list[AwardAvailability] = []
        for cabin in cabins:
            code = _CABIN_CODES[cabin]
            if not item.get(f"{code}Available"):
                continue
            airlines = [a.strip() for a in item.get(f"{code}Airlines", "").split(",") if a.strip()]

            raw_taxes = item.get(f"{code}TotalTaxes")  # int (cents) on the wire, unlike MileageCost
            if raw_taxes is None or program in _PROGRAMS_WITHOUT_TAXES:
                taxes_usd = None
            else:
                taxes_usd = _cents_to_usd(raw_taxes)

            out.append(
                AwardAvailability(
                    origin=route["OriginAirport"],
                    destination=route["DestinationAirport"],
                    date=date,
                    program=program,
                    cabin=cabin,
                    miles=int(item[f"{code}MileageCost"]),  # wire format is a string, e.g. "88000"
                    taxes_usd=taxes_usd,
                    airlines=airlines,
                    direct=bool(item.get(f"{code}Direct", False)),
                    seats=item.get(f"{code}RemainingSeats"),
                    availability_id=item["ID"],
                )
            )
        return out
