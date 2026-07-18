from __future__ import annotations

import datetime

import httpx
import pytest
import respx

from src.providers.seats_aero import (
    SeatsAeroAuthError,
    SeatsAeroClient,
    SeatsAeroRateLimitError,
    parse_trip_taxes_usd,
    select_trip_for_cabin,
)
from tests.conftest import load_fixture


@respx.mock
def test_cached_search_parses_business_awards():
    respx.get("https://seats.aero/partnerapi/search").mock(
        return_value=httpx.Response(200, json=load_fixture("seats_aero_cached_search.json"))
    )
    client = SeatsAeroClient(api_key="fake-key")

    results = client.cached_search(
        origin="IAD",
        destinations=["FCO"],
        start=datetime.date(2026, 5, 1),
        end=datetime.date(2027, 3, 1),
        cabins=["business", "first"],
    )

    assert len(results) == 2
    aeroplan, united = results
    assert aeroplan.program == "aeroplan"
    assert aeroplan.cabin == "business"
    assert aeroplan.miles == 88000
    assert aeroplan.taxes_usd == 180.0
    assert aeroplan.airlines == ["AC"]
    assert aeroplan.direct is True
    assert aeroplan.availability_id == "aeroplan-iad-fco-2026-05-14"

    assert united.program == "united"
    assert united.taxes_usd == 56.0
    assert united.airlines == ["UA", "LH"]


def test_cached_search_casts_mileage_cost_string_to_int():
    """Regression: {X}MileageCost is a string on the wire ("88000"), not a
    number. If parsing ever forgets to cast, this fails loudly instead of
    silently producing a string that breaks downstream arithmetic (e.g. the
    dedup key's `miles // 5000 * 5000` bucketing)."""
    raw_item = load_fixture("seats_aero_cached_search.json")["data"][0]
    assert isinstance(raw_item["JMileageCost"], str)  # confirms the fixture models the real wire format

    client = SeatsAeroClient(api_key="fake-key")
    parsed = client._parse_item(raw_item, ["business"])

    assert len(parsed) == 1
    award = parsed[0]
    assert award.miles == 88000
    assert isinstance(award.miles, int)
    assert not isinstance(award.miles, str)
    # would raise TypeError if `.miles` were still a string
    assert award.miles // 5000 * 5000 == 85000


def test_cached_search_taxes_field_is_int_not_string_unlike_mileage_cost():
    """Regression: {X}TotalTaxes is a plain JSON int on the wire (e.g. 18000),
    while {X}MileageCost is a string (e.g. "88000") on the very same record.
    Conflating the two -- e.g. reusing the int() cast defensively on both, or
    assuming both need it -- would either crash on a non-numeric string cast
    or mask a real type mismatch."""
    raw_item = load_fixture("seats_aero_cached_search.json")["data"][0]
    assert isinstance(raw_item["JMileageCost"], str)
    assert isinstance(raw_item["JTotalTaxes"], int)
    assert not isinstance(raw_item["JTotalTaxes"], str)

    client = SeatsAeroClient(api_key="fake-key")
    award = client._parse_item(raw_item, ["business"])[0]
    assert award.miles == 88000
    assert award.taxes_usd == 180.0


def test_cached_search_treats_known_non_reporting_program_taxes_as_none():
    """Regression: confirmed via a real live call that seats.aero represents
    a non-tax-reporting program's (singapore/KrisFlyer) taxes as a present
    but zero {X}TotalTaxes -- identical on the wire to a genuine $0 co-pay.
    We must not trust that 0 at face value for these programs."""
    raw_item = load_fixture("seats_aero_cached_search_no_tax_program.json")["data"][0]
    assert raw_item["Source"] == "singapore"
    assert raw_item["JTotalTaxes"] == 0  # present, not absent/null -- that's the whole gotcha

    client = SeatsAeroClient(api_key="fake-key")
    award = client._parse_item(raw_item, ["business"])[0]

    assert award.miles == 247500
    assert award.taxes_usd is None  # not 0.0 -- unknown, never silently treated as free


def test_parse_item_treats_missing_taxes_field_as_none_even_for_reporting_program():
    """Defensive fallback: if {X}TotalTaxes is absent entirely (not just 0)
    for a normal, tax-reporting program, that's still "unknown," not $0."""
    raw_item = load_fixture("seats_aero_cached_search.json")["data"][0]
    assert raw_item["Source"] == "aeroplan"  # a program that does report taxes
    del raw_item["JTotalTaxes"]

    client = SeatsAeroClient(api_key="fake-key")
    award = client._parse_item(raw_item, ["business"])[0]

    assert award.taxes_usd is None


@respx.mock
def test_cached_search_parses_economy_award():
    """Economy hasn't been exercised end-to-end before (v1.0/v1.1 only ever
    used business/first) -- a real-schema-shaped economy fixture (all four
    cabins' fields present, only YAvailable true, per the real wire shape),
    not just a relabeled business fixture, to actually prove the Y-prefix
    cabin-code mapping (_CABIN_CODES) works, not just that "economy" is a
    valid dict key."""
    respx.get("https://seats.aero/partnerapi/search").mock(
        return_value=httpx.Response(200, json=load_fixture("seats_aero_cached_search_economy.json"))
    )
    client = SeatsAeroClient(api_key="fake-key")

    results = client.cached_search(
        origin="IAD",
        destinations=["FCO"],
        start=datetime.date(2027, 6, 30),
        end=datetime.date(2027, 7, 14),
        cabins=["economy"],
    )

    assert len(results) == 1
    award = results[0]
    assert award.cabin == "economy"
    assert award.program == "united"
    assert award.miles == 30000
    assert isinstance(award.miles, int)  # YMileageCost is a string on the wire, same as JMileageCost
    assert award.taxes_usd == 75.50
    assert award.airlines == ["UA"]
    assert award.direct is True
    assert award.seats == 4


@respx.mock
def test_cached_search_skips_unavailable_cabins():
    respx.get("https://seats.aero/partnerapi/search").mock(
        return_value=httpx.Response(200, json=load_fixture("seats_aero_cached_search.json"))
    )
    client = SeatsAeroClient(api_key="fake-key")

    results = client.cached_search(
        origin="IAD",
        destinations=["FCO"],
        start=datetime.date(2026, 5, 1),
        end=datetime.date(2027, 3, 1),
        cabins=["first"],
    )
    # FAvailable is false on both fixture rows -> nothing should parse
    assert results == []


@respx.mock
def test_get_trips_returns_trip_detail():
    respx.get("https://seats.aero/partnerapi/trips/aeroplan-iad-fco-2026-05-14").mock(
        return_value=httpx.Response(200, json=load_fixture("seats_aero_get_trips.json"))
    )
    client = SeatsAeroClient(api_key="fake-key")

    trips = client.get_trips("aeroplan-iad-fco-2026-05-14")

    assert trips is not None
    assert len(trips) == 1
    trip = trips[0]
    assert trip["Cabin"] == "business"
    assert trip["MileageCost"] == 88000
    assert isinstance(trip["MileageCost"], int)  # Get Trips returns typed ints, unlike Cached Search
    assert trip["TotalTaxes"] == 18000


@respx.mock
def test_get_trips_returns_none_when_space_gone():
    respx.get("https://seats.aero/partnerapi/trips/vanished-trip").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    client = SeatsAeroClient(api_key="fake-key")

    assert client.get_trips("vanished-trip") is None


@respx.mock
def test_403_raises_clear_auth_error():
    respx.get("https://seats.aero/partnerapi/search").mock(return_value=httpx.Response(403))
    client = SeatsAeroClient(api_key="bad-key")

    with pytest.raises(SeatsAeroAuthError, match="Pro plan"):
        client.cached_search("IAD", ["FCO"], datetime.date(2026, 5, 1), datetime.date(2026, 6, 1), ["business"])


@respx.mock
def test_401_raises_clear_auth_error():
    respx.get("https://seats.aero/partnerapi/search").mock(return_value=httpx.Response(401))
    client = SeatsAeroClient(api_key="bad-key")

    with pytest.raises(SeatsAeroAuthError, match="Pro plan"):
        client.cached_search("IAD", ["FCO"], datetime.date(2026, 5, 1), datetime.date(2026, 6, 1), ["business"])


@respx.mock
def test_tracks_last_rate_limit_remaining_header():
    respx.get("https://seats.aero/partnerapi/search").mock(
        return_value=httpx.Response(
            200,
            json=load_fixture("seats_aero_cached_search.json"),
            headers={"X-RateLimit-Remaining": "987"},
        )
    )
    client = SeatsAeroClient(api_key="fake-key")

    assert client.last_rate_limit_remaining is None
    client.cached_search("IAD", ["FCO"], datetime.date(2026, 5, 1), datetime.date(2026, 6, 1), ["business"])

    assert client.last_rate_limit_remaining == "987"


@respx.mock
def test_429_raises_rate_limit_error_without_burning_quota():
    route = respx.get("https://seats.aero/partnerapi/search").mock(return_value=httpx.Response(429))
    client = SeatsAeroClient(api_key="fake-key", max_retries=0)

    with pytest.raises(SeatsAeroRateLimitError):
        client.cached_search("IAD", ["FCO"], datetime.date(2026, 5, 1), datetime.date(2026, 6, 1), ["business"])

    assert route.call_count == 1


def test_parse_trip_taxes_usd_matches_concepts_doc_worked_example():
    """Regression: seats.aero's own Concepts doc worked example is 70,000
    miles + $12.90 taxes, represented on the wire as MileageCost: 70000,
    TotalTaxes: 1290 (cents). A missing /100 here would silently read this
    as $1,290 instead of $12.90."""
    trip = {"MileageCost": 70000, "TotalTaxes": 1290}
    assert trip["MileageCost"] == 70000  # not cents, no conversion needed
    assert parse_trip_taxes_usd(trip) == pytest.approx(12.90)


def test_parse_trip_taxes_usd_defaults_to_zero_when_missing():
    assert parse_trip_taxes_usd({}) == 0.0


def test_client_has_no_live_search_method():
    # Live Search is commercial-partner-only and unavailable on a Pro
    # account -- confirm we didn't reintroduce it.
    assert not hasattr(SeatsAeroClient, "live_search")


def test_select_trip_for_cabin_skips_non_matching_cabins():
    """Regression: confirmed via a real live call that Get Trips returns
    itineraries across ALL cabins for one AvailabilityID (88 trips for a
    single business-cabin hit, spanning economy/premium/business/first),
    not just the cabin Cached Search matched. trips[0] is not reliable."""
    trips = [
        {"Cabin": "economy", "MileageCost": 40000},
        {"Cabin": "business", "MileageCost": 88000},
        {"Cabin": "first", "MileageCost": 150000},
    ]
    selected = select_trip_for_cabin(trips, "business")
    assert selected["MileageCost"] == 88000


def test_select_trip_for_cabin_picks_cheapest_among_matches():
    trips = [
        {"Cabin": "business", "MileageCost": 95000},
        {"Cabin": "business", "MileageCost": 88000},
        {"Cabin": "economy", "MileageCost": 1},  # decoy: cheapest overall, wrong cabin
    ]
    selected = select_trip_for_cabin(trips, "business")
    assert selected["MileageCost"] == 88000


def test_select_trip_for_cabin_returns_none_when_no_match():
    trips = [{"Cabin": "economy", "MileageCost": 40000}]
    assert select_trip_for_cabin(trips, "business") is None


def test_select_trip_for_cabin_handles_empty_list():
    assert select_trip_for_cabin([], "business") is None
