from __future__ import annotations

import httpx
import pytest
import respx

from src.notify.base import Button
from src.notify.telegram import TelegramError, TelegramNotifier, escape_markdown_v2, format_award_alert
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
    # trip has fresher numbers than the Cached Search hit; message should
    # reflect the trip's MileageCost/TotalTaxes/Cabin, not the award's.
    verdict = Verdict(fire=True, reason="saver-equivalent availability", headline="6.5¢/pt vs $5,900 cash")
    trip = {**SAMPLE_TRIP, "MileageCost": 90000, "TotalTaxes": 20000, "Cabin": "first"}
    message = format_award_alert(saver_business_award, verdict, trip)
    assert "90,000" in message
    assert "First" in message


def test_format_award_alert_includes_deep_link(saver_business_award):
    verdict = Verdict(fire=True, reason="saver-equivalent availability", headline="6.5¢/pt vs $5,900 cash")
    message = format_award_alert(saver_business_award, verdict, SAMPLE_TRIP, deep_link="https://example.com/book")
    assert "https://example.com/book" in message
    assert "Book on Aeroplan" in message


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
def test_telegram_send_raises_on_failure():
    respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
        return_value=httpx.Response(400, text="Bad Request: can't parse entities")
    )
    notifier = TelegramNotifier("test-token", "12345")
    with pytest.raises(TelegramError):
        notifier.send("hello")
