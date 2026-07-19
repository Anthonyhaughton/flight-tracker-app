from __future__ import annotations

import httpx
import pytest
import respx

import datetime

from src.digest import DigestEntry, DigestResult
from src.notify.base import Button
from src.notify.telegram import (
    TelegramError,
    TelegramNotifier,
    escape_markdown_v2,
    format_award_alert,
    format_cash_alert,
    format_digest_alert,
)
from src.providers.cash.base import CashFare
from src.providers.seats_aero import AwardAvailability
from src.state import Baseline
from src.valuation import Verdict

SAMPLE_TRIP = {
    "ID": "aeroplan-iad-fco-2026-05-14-trip1",
    "AvailabilityID": "aeroplan-iad-fco-2026-05-14",
    "MileageCost": 88000,
    "TotalTaxes": 18000,
    "Cabin": "business",
    "RemainingSeats": 2,
    "FlightNumbers": "AC942",
}

SAMPLE_FARE = CashFare(
    origin="IAD", destination="FCO", date=datetime.date(2026, 9, 14), return_date=None,
    cabin="business", price_usd=4500.0, airline="United", stops=0, deep_link=None,
)
SAMPLE_BASELINE = Baseline(
    trailing_min_usd=5500.0, ema_usd=6000.0, updated_at=datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc)
)
SAMPLE_CASH_VERDICT = Verdict(fire=True, reason="cash price drop", headline="$4,500 vs $6,000 baseline (-25%)")


def test_escape_markdown_v2_escapes_all_special_chars():
    raw = "_*[]()~`>#+-=|{}.!"
    escaped = escape_markdown_v2(raw)
    assert escaped == "".join(f"\\{c}" for c in raw)


def test_escape_markdown_v2_handles_price_and_date():
    # the classic footgun: unescaped '.' and '-' 400 the request
    assert escape_markdown_v2("$1,234.50") == r"$1,234\.50"
    assert escape_markdown_v2("2026-05-14") == r"2026\-05\-14"


def test_format_award_alert_escapes_dynamic_values(saver_business_award):
    verdict = Verdict(fire=True, reason="saver-equivalent availability", headline="6.5¢/pt vs $5,900 cash")
    message = format_award_alert(saver_business_award, verdict, SAMPLE_TRIP)
    assert "IAD" in message
    assert "FCO" in message
    # date's dots/dashes must be escaped, not raw
    assert "2026\\-05\\-14" in message
    assert "88,000" in message
    assert "\\." in message  # the cash figure's decimal point got escaped


def test_format_award_alert_uses_trip_detail_for_miles_and_taxes(saver_business_award):
    # trip has fresher miles/taxes than the Cached Search hit; message
    # should reflect those. Cabin is a separate story -- see the next test.
    verdict = Verdict(fire=True, reason="saver-equivalent availability", headline="6.5¢/pt vs $5,900 cash")
    trip = {**SAMPLE_TRIP, "MileageCost": 90000, "TotalTaxes": 20000}
    message = format_award_alert(saver_business_award, verdict, trip)
    assert "90,000" in message


def test_format_award_alert_uses_award_cabin_not_trip_cabin(saver_business_award):
    """Regression (found in the first live dry run): Get Trips returns
    itineraries across ALL cabins for an AvailabilityID, not just the one
    Cached Search matched -- trusting trip["Cabin"] showed the wrong cabin.
    The message must reflect award.cabin ("business" here), even when
    handed a mismatched trip."""
    verdict = Verdict(fire=True, reason="saver-equivalent availability", headline="6.5¢/pt vs $5,900 cash")
    mismatched_trip = {**SAMPLE_TRIP, "Cabin": "first"}
    message = format_award_alert(saver_business_award, verdict, mismatched_trip)
    assert "Business" in message
    assert "First" not in message


def test_format_award_alert_includes_deep_link(saver_business_award):
    verdict = Verdict(fire=True, reason="saver-equivalent availability", headline="6.5¢/pt vs $5,900 cash")
    message = format_award_alert(saver_business_award, verdict, SAMPLE_TRIP, deep_link="https://example.com/book")
    assert "https://example.com/book" in message
    assert "Book on Aeroplan" in message


# --- format_cash_alert ---


def test_format_cash_alert_escapes_dynamic_values():
    message = format_cash_alert(SAMPLE_FARE, SAMPLE_CASH_VERDICT, SAMPLE_BASELINE)
    assert "IAD" in message
    assert "FCO" in message
    assert "2026\\-09\\-14" in message  # date's dashes must be escaped
    assert "4,500" in message
    assert "\\-25%" in message  # the drop%'s leading '-' must be escaped for MarkdownV2


def test_format_cash_alert_shows_price_and_baseline():
    message = format_cash_alert(SAMPLE_FARE, SAMPLE_CASH_VERDICT, SAMPLE_BASELINE)
    assert "4,500" in message
    assert "6,000" in message
    assert "25%" in message


def test_format_cash_alert_includes_deep_link_when_present():
    import dataclasses

    fare_with_link = dataclasses.replace(SAMPLE_FARE, deep_link="https://example.com/book")
    message = format_cash_alert(fare_with_link, SAMPLE_CASH_VERDICT, SAMPLE_BASELINE)
    assert "https://example.com/book" in message


def test_format_cash_alert_omits_link_line_when_absent():
    message = format_cash_alert(SAMPLE_FARE, SAMPLE_CASH_VERDICT, SAMPLE_BASELINE)  # deep_link=None
    assert "Book flight" not in message


def test_format_cash_alert_handles_none_baseline():
    # The absolute mistake-fare-ceiling trigger can fire before any baseline
    # exists -- must format cleanly (not crash on None.ema_usd).
    ceiling_verdict = Verdict(fire=True, reason="possible mistake fare (absolute ceiling)", headline="$150 one-way")
    message = format_cash_alert(SAMPLE_FARE, ceiling_verdict, None)
    assert "4,500" in message
    assert "baseline" not in message.lower()


@respx.mock
def test_telegram_send_cash_alert_formats_and_sends():
    route = respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    notifier = TelegramNotifier("test-token", "12345")

    notifier.send_cash_alert(SAMPLE_FARE, SAMPLE_CASH_VERDICT, SAMPLE_BASELINE)

    assert route.called
    assert b"IAD" in route.calls[0].request.content


@respx.mock
def test_telegram_send_success():
    route = respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    notifier = TelegramNotifier("test-token", "12345")
    notifier.send("hello")
    assert route.called
    sent_payload = route.calls[0].request
    assert b'"chat_id":"12345"' in sent_payload.content
    assert b'"parse_mode":"MarkdownV2"' in sent_payload.content


@respx.mock
def test_telegram_send_includes_buttons():
    route = respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    notifier = TelegramNotifier("test-token", "12345")
    notifier.send("hello", buttons=[Button(text="Book", url="https://example.com")])
    assert b"inline_keyboard" in route.calls[0].request.content


@respx.mock
def test_telegram_send_award_alert_formats_and_sends(saver_business_award):
    route = respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    notifier = TelegramNotifier("test-token", "12345")
    verdict = Verdict(fire=True, reason="saver-equivalent availability", headline="6.5¢/pt vs $5,900 cash")

    notifier.send_award_alert(saver_business_award, verdict, SAMPLE_TRIP)

    assert route.called
    assert b"IAD" in route.calls[0].request.content


@respx.mock
def test_telegram_send_raises_on_failure():
    respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
        return_value=httpx.Response(400, text="Bad Request: can't parse entities")
    )
    notifier = TelegramNotifier("test-token", "12345")
    with pytest.raises(TelegramError):
        notifier.send("hello")


# --- format_digest_alert / send_digest ---

_DIGEST_AWARD = AwardAvailability(
    origin="IAD", destination="FCO", date=datetime.date(2026, 5, 14), program="aeroplan", cabin="business",
    miles=88000, taxes_usd=180.0, airlines=["AC"], direct=True, seats=2,
    availability_id="aeroplan-iad-fco-2026-05-14",
)


def _digest_entry(**overrides) -> DigestEntry:
    defaults = dict(
        award=_DIGEST_AWARD,
        route=None,
        comparable_cash_usd=5900.0,
        taxes_usd=180.0,
        cpp=6.5,
        trip_value_usd=5720.0,
        real_time_cpp_floor=2.0,
        confirmed=True,
        cleared_real_time_bar=True,
    )
    defaults.update(overrides)
    return DigestEntry(**defaults)


def test_format_digest_alert_empty_case_when_nothing_ranked():
    result = DigestResult(cash_rank=[], cpp_rank=[], candidates_evaluated=42, candidates_ranked=0)

    message = format_digest_alert(result)

    assert "No award availability found this week" in message
    assert "42" in message


def test_format_digest_alert_escapes_and_includes_both_sections():
    entry = _digest_entry()
    result = DigestResult(cash_rank=[entry], cpp_rank=[entry], candidates_evaluated=1, candidates_ranked=1)

    message = format_digest_alert(result)

    assert "Top Cash Value" in message
    assert "Top CPP" in message
    assert "IAD" in message and "FCO" in message
    assert "2026\\-05\\-14" in message  # date escaping, same MarkdownV2 rule as the real-time formatters
    assert "6\\.5" in message  # cpp figure's decimal escaped


def test_format_digest_alert_notes_real_time_match():
    entry = _digest_entry(cleared_real_time_bar=True)
    result = DigestResult(cash_rank=[entry], cpp_rank=[entry], candidates_evaluated=1, candidates_ranked=1)

    message = format_digest_alert(result)

    assert "⭐" in message


def test_format_digest_alert_notes_when_nothing_cleared_real_time_bar():
    entry = _digest_entry(cleared_real_time_bar=False, cpp=1.2, real_time_cpp_floor=2.0)
    result = DigestResult(cash_rank=[entry], cpp_rank=[entry], candidates_evaluated=1, candidates_ranked=1)

    message = format_digest_alert(result)

    assert "Nothing cleared the real" in message
    assert "1\\.2" in message
    assert "2\\.0" in message
    assert "⭐" not in message


@respx.mock
def test_send_digest_success():
    route = respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    notifier = TelegramNotifier("test-token", "12345")
    entry = _digest_entry()
    result = DigestResult(cash_rank=[entry], cpp_rank=[entry], candidates_evaluated=1, candidates_ranked=1)

    notifier.send_digest(result)

    assert route.called
    assert b"Weekly Deal Digest" in route.calls[0].request.content
