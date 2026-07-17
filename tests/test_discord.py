from __future__ import annotations

import httpx
import pytest
import respx

import datetime

from src.notify.discord import (
    COLOR_CASH_DEAL,
    COLOR_GOOD_DEAL,
    DiscordError,
    DiscordNotifier,
    DiscordValidationError,
    format_award_embed,
    format_cash_embed,
)
from src.providers.cash.base import CashFare
from src.state import Baseline
from src.valuation import Verdict

FAKE_WEBHOOK_URL = "https://discord.com/api/webhooks/123456789/fake-token-not-real"

SAMPLE_TRIP = {
    "ID": "aeroplan-iad-fco-2026-05-14-trip1",
    "AvailabilityID": "aeroplan-iad-fco-2026-05-14",
    "MileageCost": 88000,
    "TotalTaxes": 18000,
    "Cabin": "business",
    "RemainingSeats": 2,
    "FlightNumbers": "AC942",
}

SAMPLE_VERDICT = Verdict(fire=True, reason="saver-equivalent availability", headline="6.5¢/pt vs $5,900 cash")

SAMPLE_FARE = CashFare(
    origin="IAD", destination="FCO", date=datetime.date(2026, 9, 14), return_date=None,
    cabin="business", price_usd=4500.0, airline="United", stops=0, deep_link=None,
)
SAMPLE_BASELINE = Baseline(
    trailing_min_usd=5500.0, ema_usd=6000.0, updated_at=datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc)
)
SAMPLE_CASH_VERDICT = Verdict(fire=True, reason="cash price drop", headline="$4,500 vs $6,000 baseline (-25%)")


# --- format_award_embed: structure ---


def test_format_award_embed_structure(saver_business_award):
    embed = format_award_embed(saver_business_award, SAMPLE_VERDICT, SAMPLE_TRIP)

    assert embed["title"] == "Business - IAD -> FCO"
    assert embed["color"] == COLOR_GOOD_DEAL
    assert isinstance(embed["color"], int)

    fields_by_name = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields_by_name["Program"] == "Aeroplan"
    assert fields_by_name["Date"] == "2026-05-14"
    assert fields_by_name["Miles"] == "88,000"
    assert fields_by_name["Taxes (USD)"] == "$180.00"
    assert fields_by_name["Seats"] == "2"
    assert fields_by_name["Nonstop"] == "Yes"
    assert all(f["inline"] is True for f in embed["fields"])

    assert embed["footer"]["text"] == SAMPLE_VERDICT.headline
    assert "timestamp" in embed
    assert "url" not in embed  # no deep_link supplied


def test_format_award_embed_includes_url_when_deep_link_given(saver_business_award):
    embed = format_award_embed(saver_business_award, SAMPLE_VERDICT, SAMPLE_TRIP, deep_link="https://example.com/book")
    assert embed["url"] == "https://example.com/book"


def test_format_award_embed_title_uses_award_cabin_not_trip_cabin(saver_business_award):
    """Regression (found in the first live dry run): Get Trips returns
    itineraries across ALL cabins for an AvailabilityID, not just the one
    Cached Search matched -- a business-cabin award's title showed "Economy"
    because trip["Cabin"] was blindly trusted. The title must reflect
    award.cabin ("business" here), even when handed a mismatched trip."""
    mismatched_trip = {**SAMPLE_TRIP, "Cabin": "economy"}
    embed = format_award_embed(saver_business_award, SAMPLE_VERDICT, mismatched_trip)
    assert embed["title"] == "Business - IAD -> FCO"
    assert "Economy" not in embed["title"]


def test_format_award_embed_marks_connecting_flights(saver_business_award):
    import dataclasses

    connecting_award = dataclasses.replace(saver_business_award, direct=False)
    embed = format_award_embed(connecting_award, SAMPLE_VERDICT, SAMPLE_TRIP)
    fields_by_name = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields_by_name["Nonstop"] == "No"


def test_format_award_embed_omits_seats_field_when_unknown(saver_business_award):
    trip_without_seats = {**SAMPLE_TRIP, "RemainingSeats": None}
    embed = format_award_embed(saver_business_award, SAMPLE_VERDICT, trip_without_seats)
    names = [f["name"] for f in embed["fields"]]
    assert "Seats" not in names


def test_format_award_embed_no_markdown_escaping_needed(saver_business_award):
    # Discord embeds render plain text -- a literal '.' or '-' must NOT be
    # backslash-escaped the way Telegram's MarkdownV2 requires.
    embed = format_award_embed(saver_business_award, SAMPLE_VERDICT, SAMPLE_TRIP)
    assert embed["title"] == "Business - IAD -> FCO"  # no "\\-"
    fields_by_name = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields_by_name["Date"] == "2026-05-14"  # no "\\-"
    assert fields_by_name["Taxes (USD)"] == "$180.00"  # no "\\."


# --- format_cash_embed: structure, distinct from award embeds ---


def test_format_cash_embed_structure():
    embed = format_cash_embed(SAMPLE_FARE, SAMPLE_CASH_VERDICT, SAMPLE_BASELINE)

    assert embed["title"] == "Cash drop - Business IAD -> FCO"
    assert embed["color"] == COLOR_CASH_DEAL
    assert embed["color"] != COLOR_GOOD_DEAL  # visually distinct from award alerts

    fields_by_name = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields_by_name["Cabin"] == "Business"
    assert fields_by_name["Date"] == "2026-09-14"
    assert fields_by_name["Price"] == "$4,500"
    assert fields_by_name["Baseline (typical)"] == "$6,000"
    assert fields_by_name["Lowest seen"] == "$5,500"
    assert fields_by_name["Drop"] == "-25%"
    assert fields_by_name["Airline"] == "United"
    assert fields_by_name["Stops"] == "0"

    assert embed["footer"]["text"] == SAMPLE_CASH_VERDICT.headline
    assert "url" not in embed  # SAMPLE_FARE.deep_link is None


def test_format_cash_embed_has_no_award_shaped_fields():
    # A cash alert must not carry award-specific fields (Program, Miles,
    # Taxes) -- this is a different kind of alert, not a redemption.
    embed = format_cash_embed(SAMPLE_FARE, SAMPLE_CASH_VERDICT, SAMPLE_BASELINE)
    names = {f["name"] for f in embed["fields"]}
    assert names.isdisjoint({"Program", "Miles", "Taxes (USD)", "Nonstop"})


def test_format_cash_embed_includes_url_when_fare_has_deep_link():
    import dataclasses

    fare_with_link = dataclasses.replace(SAMPLE_FARE, deep_link="https://example.com/book")
    embed = format_cash_embed(fare_with_link, SAMPLE_CASH_VERDICT, SAMPLE_BASELINE)
    assert embed["url"] == "https://example.com/book"


def test_format_cash_embed_drop_percent_uses_previous_baseline_not_current_price():
    # The drop % must be computed against the baseline BEFORE this
    # observation -- comparing a price against a baseline already blended
    # with itself would understate every drop.
    embed = format_cash_embed(SAMPLE_FARE, SAMPLE_CASH_VERDICT, SAMPLE_BASELINE)
    fields_by_name = {f["name"]: f["value"] for f in embed["fields"]}
    # (6000 - 4500) / 6000 = 25%
    assert fields_by_name["Drop"] == "-25%"


@respx.mock
def test_send_cash_alert_success_on_204():
    route = respx.post(FAKE_WEBHOOK_URL).mock(return_value=httpx.Response(204))
    notifier = DiscordNotifier(FAKE_WEBHOOK_URL)

    notifier.send_cash_alert(SAMPLE_FARE, SAMPLE_CASH_VERDICT, SAMPLE_BASELINE)

    assert route.called
    import json

    payload = json.loads(route.calls[0].request.content)
    assert payload["embeds"][0]["title"] == "Cash drop - Business IAD -> FCO"


# --- send_award_alert / _send_embeds: success (204) ---


@respx.mock
def test_send_award_alert_success_on_204(saver_business_award):
    route = respx.post(FAKE_WEBHOOK_URL).mock(return_value=httpx.Response(204))
    notifier = DiscordNotifier(FAKE_WEBHOOK_URL)

    notifier.send_award_alert(saver_business_award, SAMPLE_VERDICT, SAMPLE_TRIP)

    assert route.called
    body = route.calls[0].request.content
    import json

    payload = json.loads(body)
    assert isinstance(payload["embeds"], list)
    assert len(payload["embeds"]) == 1
    assert payload["embeds"][0]["title"] == "Business - IAD -> FCO"


@respx.mock
def test_send_award_alert_200_is_not_treated_as_success(saver_business_award):
    # Discord's success code is 204 No Content, not 200 -- a webhook that
    # somehow returned 200 must still be treated as a failure.
    respx.post(FAKE_WEBHOOK_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    notifier = DiscordNotifier(FAKE_WEBHOOK_URL)

    with pytest.raises(DiscordError):
        notifier.send_award_alert(saver_business_award, SAMPLE_VERDICT, SAMPLE_TRIP)


# --- validation: empty field filtering ---


def test_send_embeds_filters_empty_and_none_field_values():
    notifier = DiscordNotifier(FAKE_WEBHOOK_URL)
    embed = {
        "title": "Test",
        "fields": [
            {"name": "Program", "value": "Aeroplan", "inline": True},
            {"name": "Empty", "value": "", "inline": True},
            {"name": "Missing", "value": None, "inline": True},
        ],
    }

    from src.notify.discord import _validate_and_clean_embeds

    cleaned = _validate_and_clean_embeds([embed])

    names = [f["name"] for f in cleaned[0]["fields"]]
    assert names == ["Program"]


@respx.mock
def test_send_embeds_never_posts_empty_field_values(saver_business_award):
    # end-to-end: a trip missing RemainingSeats must not produce a request
    # with an empty/None "Seats" field on the wire.
    route = respx.post(FAKE_WEBHOOK_URL).mock(return_value=httpx.Response(204))
    notifier = DiscordNotifier(FAKE_WEBHOOK_URL)
    trip_without_seats = {**SAMPLE_TRIP, "RemainingSeats": None}

    notifier.send_award_alert(saver_business_award, SAMPLE_VERDICT, trip_without_seats)

    import json

    payload = json.loads(route.calls[0].request.content)
    names = [f["name"] for f in payload["embeds"][0]["fields"]]
    assert "Seats" not in names


# --- validation: size/count limits ---


def test_send_embeds_rejects_over_6000_total_chars():
    notifier = DiscordNotifier(FAKE_WEBHOOK_URL)
    huge_embed = {
        "title": "Test",
        "fields": [{"name": "Field", "value": "x" * 1024, "inline": True} for _ in range(6)],
    }
    # 6 * 1024 = 6144 > 6000
    with pytest.raises(DiscordValidationError, match="6000"):
        notifier._send_embeds([huge_embed])


def test_send_embeds_rejects_title_over_256_chars():
    notifier = DiscordNotifier(FAKE_WEBHOOK_URL)
    embed = {"title": "x" * 257, "fields": []}
    with pytest.raises(DiscordValidationError, match="256"):
        notifier._send_embeds([embed])


def test_send_embeds_rejects_field_value_over_1024_chars():
    notifier = DiscordNotifier(FAKE_WEBHOOK_URL)
    embed = {"title": "Test", "fields": [{"name": "Field", "value": "x" * 1025, "inline": True}]}
    with pytest.raises(DiscordValidationError, match="1024"):
        notifier._send_embeds([embed])


def test_send_embeds_rejects_more_than_25_fields():
    notifier = DiscordNotifier(FAKE_WEBHOOK_URL)
    embed = {"title": "Test", "fields": [{"name": f"F{i}", "value": "x", "inline": True} for i in range(26)]}
    with pytest.raises(DiscordValidationError, match="25"):
        notifier._send_embeds([embed])


def test_send_embeds_rejects_more_than_10_embeds():
    notifier = DiscordNotifier(FAKE_WEBHOOK_URL)
    embeds = [{"title": f"Embed {i}", "fields": []} for i in range(11)]
    with pytest.raises(DiscordValidationError, match="10"):
        notifier._send_embeds(embeds)


def test_send_embeds_rejects_non_list_embeds():
    notifier = DiscordNotifier(FAKE_WEBHOOK_URL)
    with pytest.raises(DiscordValidationError, match="list"):
        notifier._send_embeds({"title": "not a list"})  # type: ignore[arg-type]


@respx.mock
def test_validation_failure_never_sends_a_request():
    # A malformed embed must be rejected locally -- confirmed here by not
    # registering any respx route at all; if the code tried to POST anyway,
    # respx would raise for the unmatched request instead of DiscordValidationError.
    notifier = DiscordNotifier(FAKE_WEBHOOK_URL)
    huge_embed = {"title": "x" * 257, "fields": []}

    with pytest.raises(DiscordValidationError):
        notifier._send_embeds([huge_embed])


# --- 429 handling ---


@respx.mock
def test_send_embeds_retries_after_429_then_succeeds(saver_business_award):
    route = respx.post(FAKE_WEBHOOK_URL).mock(
        side_effect=[
            httpx.Response(429, json={"retry_after": 0.01}),
            httpx.Response(204),
        ]
    )
    notifier = DiscordNotifier(FAKE_WEBHOOK_URL, max_retries=1)

    notifier.send_award_alert(saver_business_award, SAMPLE_VERDICT, SAMPLE_TRIP)

    assert route.call_count == 2


@respx.mock
def test_send_embeds_raises_after_exhausting_429_retries(saver_business_award):
    route = respx.post(FAKE_WEBHOOK_URL).mock(return_value=httpx.Response(429, json={"retry_after": 0.01}))
    notifier = DiscordNotifier(FAKE_WEBHOOK_URL, max_retries=0)

    with pytest.raises(DiscordError):
        notifier.send_award_alert(saver_business_award, SAMPLE_VERDICT, SAMPLE_TRIP)

    assert route.call_count == 1


@respx.mock
def test_send_embeds_falls_back_to_retry_after_header(saver_business_award):
    route = respx.post(FAKE_WEBHOOK_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0.01"}, content=b""),
            httpx.Response(204),
        ]
    )
    notifier = DiscordNotifier(FAKE_WEBHOOK_URL, max_retries=1)

    notifier.send_award_alert(saver_business_award, SAMPLE_VERDICT, SAMPLE_TRIP)

    assert route.call_count == 2


# --- non-204/429 failures must raise and never be recorded as sent ---


@respx.mock
def test_send_award_alert_raises_on_non_204_non_429(saver_business_award):
    respx.post(FAKE_WEBHOOK_URL).mock(return_value=httpx.Response(400, text='{"message": "Invalid Form Body"}'))
    notifier = DiscordNotifier(FAKE_WEBHOOK_URL)

    with pytest.raises(DiscordError, match="400"):
        notifier.send_award_alert(saver_business_award, SAMPLE_VERDICT, SAMPLE_TRIP)


@respx.mock
def test_poller_does_not_record_alert_when_discord_send_raises(saver_business_award):
    """Full loop: a real DiscordNotifier failure must propagate up through
    the poller and leave dedup state untouched, so the next poll retries
    rather than silently believing the alert went out."""
    from src.poller import poll_route
    from src.config import AwardConfig, DateWindow, RouteConfig
    from src.state import InMemoryStateStore, award_key

    respx.post(FAKE_WEBHOOK_URL).mock(return_value=httpx.Response(500, text="Internal Server Error"))
    notifier = DiscordNotifier(FAKE_WEBHOOK_URL)

    class FakeSeatsClient:
        def cached_search(self, origin, destinations, start, end, cabins):
            return [saver_business_award]

        def get_trips(self, availability_id):
            return [SAMPLE_TRIP]

    route = RouteConfig(
        name="DC → Italy",
        destinations=["FCO"],
        cabins=["business", "first"],
        date_window=DateWindow(start_offset=30, end_offset=330),
        active=True,
    )
    config_awards = AwardConfig(min_trip_value_usd=1500, cpp_floors={"default": 1.4})

    class FakeAlertsConfig:
        dedup_ttl_days = 5

    class FakeConfig:
        awards = config_awards
        alerts = FakeAlertsConfig()

    state = InMemoryStateStore()

    with pytest.raises(DiscordError):
        poll_route(FakeSeatsClient(), ["IAD"], route, FakeConfig(), state, notifier)

    assert state.already_alerted(award_key(saver_business_award)) is False
