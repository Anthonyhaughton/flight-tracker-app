from __future__ import annotations

import datetime

import httpx
import pytest
import respx

from src.providers.cash.serpapi import SerpApiAuthError, SerpApiClient, SerpApiRateLimitError
from tests.conftest import load_fixture


@respx.mock
def test_search_parses_cheapest_one_way_itinerary():
    """best_flights has the $5,843 nonstop; other_flights has a pricier
    1-stop alternative -- the cheapest across BOTH arrays wins."""
    respx.get("https://serpapi.com/search").mock(
        return_value=httpx.Response(200, json=load_fixture("serpapi_google_flights_one_way.json"))
    )
    client = SerpApiClient(api_key="fake-key")
    fares = client.search("IAD", ["FCO"], datetime.date(2026, 9, 14), datetime.date(2026, 9, 14), "business")

    assert len(fares) == 1
    fare = fares[0]
    assert fare.origin == "IAD"
    assert fare.destination == "FCO"
    assert fare.date == datetime.date(2026, 9, 14)
    assert fare.cabin == "business"
    assert fare.price_usd == 5843.0
    assert fare.airline == "United"
    assert fare.stops == 0  # single leg in best_flights[0].flights


@respx.mock
def test_search_picks_cheapest_across_best_and_other_flights():
    fixture = load_fixture("serpapi_google_flights_one_way.json")
    # swap so other_flights is actually cheaper, to confirm we don't just
    # always take best_flights[0] blindly
    fixture["other_flights"][0]["price"] = 1000
    respx.get("https://serpapi.com/search").mock(return_value=httpx.Response(200, json=fixture))
    client = SerpApiClient(api_key="fake-key")
    fares = client.search("IAD", ["FCO"], datetime.date(2026, 9, 14), datetime.date(2026, 9, 14), "business")

    assert fares[0].price_usd == 1000.0
    assert fares[0].stops == 1  # the 2-leg CDG-connection itinerary


# --- one-way vs round-trip: the directionality bug this task is explicitly
# worried about (a round-trip cash price would silently inflate every CPP
# number without any test ever failing on its own) ---


@respx.mock
def test_request_uses_one_way_type_and_no_return_date():
    route = respx.get("https://serpapi.com/search").mock(
        return_value=httpx.Response(200, json=load_fixture("serpapi_google_flights_one_way.json"))
    )
    client = SerpApiClient(api_key="fake-key")
    client.search("IAD", ["FCO"], datetime.date(2026, 9, 14), datetime.date(2026, 9, 14), "business")

    query = dict(httpx.QueryParams(route.calls[0].request.url.query))
    assert query["type"] == "2"  # one-way, per SerpApi's Google Flights API reference
    assert "return_date" not in query


@respx.mock
def test_parsed_fare_never_has_a_return_date():
    respx.get("https://serpapi.com/search").mock(
        return_value=httpx.Response(200, json=load_fixture("serpapi_google_flights_one_way.json"))
    )
    client = SerpApiClient(api_key="fake-key")
    fares = client.search("IAD", ["FCO"], datetime.date(2026, 9, 14), datetime.date(2026, 9, 14), "business")

    assert fares[0].return_date is None


@respx.mock
def test_discards_itinerary_that_is_not_one_way():
    """Defensive guard: even though the request asked for type=2, if SerpApi
    ever echoes a non-"One way" itinerary (a data anomaly, a future API
    change), it must be discarded rather than silently used as a one-way
    price -- that's exactly the failure mode that would inflate CPP without
    any test-shaped signal that something's wrong."""
    fixture = load_fixture("serpapi_google_flights_one_way.json")
    fixture["best_flights"][0]["type"] = "Round trip"
    fixture["best_flights"][0]["price"] = 1  # deliberately cheapest, to prove it's excluded, not just deprioritized
    respx.get("https://serpapi.com/search").mock(return_value=httpx.Response(200, json=fixture))
    client = SerpApiClient(api_key="fake-key")
    fares = client.search("IAD", ["FCO"], datetime.date(2026, 9, 14), datetime.date(2026, 9, 14), "business")

    assert len(fares) == 1
    assert fares[0].price_usd == 6120.0  # the other_flights One-way itinerary, not the $1 round-trip decoy


@respx.mock
def test_discards_itinerary_when_all_results_are_non_one_way():
    fixture = load_fixture("serpapi_google_flights_one_way.json")
    fixture["best_flights"][0]["type"] = "Round trip"
    fixture["other_flights"][0]["type"] = "Round trip"
    respx.get("https://serpapi.com/search").mock(return_value=httpx.Response(200, json=fixture))
    client = SerpApiClient(api_key="fake-key")
    fares = client.search("IAD", ["FCO"], datetime.date(2026, 9, 14), datetime.date(2026, 9, 14), "business")

    assert fares == []


# --- travel_class mapping ---


@respx.mock
def test_request_maps_cabin_to_travel_class():
    route = respx.get("https://serpapi.com/search").mock(
        return_value=httpx.Response(200, json=load_fixture("serpapi_google_flights_one_way.json"))
    )
    client = SerpApiClient(api_key="fake-key")
    client.search("IAD", ["FCO"], datetime.date(2026, 9, 14), datetime.date(2026, 9, 14), "business")

    query = dict(httpx.QueryParams(route.calls[0].request.url.query))
    assert query["travel_class"] == "3"  # business, per SerpApi's reference


def test_search_rejects_unknown_cabin():
    client = SerpApiClient(api_key="fake-key")
    with pytest.raises(ValueError, match="premium_first"):
        client.search("IAD", ["FCO"], datetime.date(2026, 9, 14), datetime.date(2026, 9, 14), "premium_first")


# --- empty / error responses -- must log and skip, never crash the poll ---


@respx.mock
def test_search_returns_empty_list_when_no_flights_found():
    fixture = load_fixture("serpapi_google_flights_one_way.json")
    fixture["best_flights"] = []
    fixture["other_flights"] = []
    respx.get("https://serpapi.com/search").mock(return_value=httpx.Response(200, json=fixture))
    client = SerpApiClient(api_key="fake-key")
    fares = client.search("IAD", ["FCO"], datetime.date(2026, 9, 14), datetime.date(2026, 9, 14), "business")

    assert fares == []


@respx.mock
def test_search_returns_empty_list_on_query_level_error_status():
    """A 200 OK whose search_metadata.status is "Error" (e.g. an invalid
    airport pairing) -- SerpApi's documented Search API error pattern,
    distinct from an HTTP-level 401/429."""
    fixture = {"search_metadata": {"status": "Error"}, "error": "Invalid arrival_id"}
    respx.get("https://serpapi.com/search").mock(return_value=httpx.Response(200, json=fixture))
    client = SerpApiClient(api_key="fake-key")
    fares = client.search("IAD", ["FCO"], datetime.date(2026, 9, 14), datetime.date(2026, 9, 14), "business")

    assert fares == []


@respx.mock
def test_401_raises_serpapi_auth_error():
    respx.get("https://serpapi.com/search").mock(return_value=httpx.Response(401, json={"error": "Invalid API key."}))
    client = SerpApiClient(api_key="bad-key")
    with pytest.raises(SerpApiAuthError, match="401"):
        client.search("IAD", ["FCO"], datetime.date(2026, 9, 14), datetime.date(2026, 9, 14), "business")


@respx.mock
def test_429_raises_serpapi_rate_limit_error():
    respx.get("https://serpapi.com/search").mock(
        return_value=httpx.Response(429, json={"error": "Your account has run out of searches."})
    )
    client = SerpApiClient(api_key="fake-key", max_retries=0)
    with pytest.raises(SerpApiRateLimitError, match="429"):
        client.search("IAD", ["FCO"], datetime.date(2026, 9, 14), datetime.date(2026, 9, 14), "business")


@respx.mock
def test_deep_link_is_none_not_a_booking_token():
    """booking_token needs a second, separately billed SerpApi call to
    resolve into a real URL -- not fetched here, so deep_link must be None,
    not the raw opaque token (which isn't a usable URL)."""
    respx.get("https://serpapi.com/search").mock(
        return_value=httpx.Response(200, json=load_fixture("serpapi_google_flights_one_way.json"))
    )
    client = SerpApiClient(api_key="fake-key")
    fares = client.search("IAD", ["FCO"], datetime.date(2026, 9, 14), datetime.date(2026, 9, 14), "business")

    assert fares[0].deep_link is None
