"""Telegram Bot API notifier: MarkdownV2 formatting/escaping + sending.

Not spamming lives in valuation.py's dedup; this module only formats and
sends (see .claude/skills/telegram-alerting).
"""

from __future__ import annotations

import re

import httpx

from src.notify.base import Button
from src.providers.cash.base import CashFare
from src.providers.seats_aero import AwardAvailability, parse_trip_taxes_usd
from src.state import Baseline
from src.valuation import Verdict

_MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!"


def escape_markdown_v2(text) -> str:
    return re.sub(f"([{re.escape(_MDV2_SPECIAL)}])", r"\\\1", str(text))


class TelegramError(RuntimeError):
    pass


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, *, client: httpx.Client | None = None):
        self._token = bot_token
        self._chat_id = chat_id
        self._client = client or httpx.Client(timeout=10.0)

    def send(self, message: str, buttons: list[Button] | None = None) -> None:
        payload = {
            "chat_id": self._chat_id,
            "text": message,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[_button_to_dict(b) for b in buttons]]}
        response = self._client.post(f"https://api.telegram.org/bot{self._token}/sendMessage", json=payload)
        if response.status_code != 200:
            raise TelegramError(f"Telegram send failed ({response.status_code}): {response.text}")

    def send_award_alert(
        self, award: AwardAvailability, verdict: Verdict, trip: dict, *, deep_link: str | None = None
    ) -> None:
        self.send(format_award_alert(award, verdict, trip, deep_link=deep_link))

    def send_cash_alert(self, fare: CashFare, verdict: Verdict, baseline: Baseline) -> None:
        self.send(format_cash_alert(fare, verdict, baseline))

    def close(self) -> None:
        self._client.close()


def _button_to_dict(button: Button) -> dict:
    d: dict = {"text": button.text}
    if button.url:
        d["url"] = button.url
    if button.callback_data:
        d["callback_data"] = button.callback_data
    return d


def format_award_alert(award: AwardAvailability, verdict: Verdict, trip: dict, *, deep_link: str | None = None) -> str:
    """`trip` is one entry from SeatsAeroClient.get_trips(award.availability_id),
    already filtered to award.cabin by select_trip_for_cabin() in poller.py
    (Get Trips returns itineraries across ALL cabins on the availability, so
    trip["Cabin"] can't be trusted blindly). The cabin label uses award.cabin
    directly, not trip's own Cabin field.

    No "saver" in the message: there's no per-item saver flag on the wire --
    saver-equivalence comes entirely from the Cached Search request-time
    filter, so claiming it per-item here would assert something we can't
    verify."""
    esc = escape_markdown_v2
    stops = "nonstop" if award.direct else "connecting"
    program_label = award.program.replace("_", " ").title()
    cabin_label = award.cabin.title()
    miles = int(trip["MileageCost"])
    taxes_usd = parse_trip_taxes_usd(trip)

    lines = [
        f"\U0001F3AF *{esc(cabin_label)}* {esc(award.origin)} → {esc(award.destination)}",
        f"\U0001F4C5 {esc(award.date.isoformat())} \\({esc(stops)}\\)",
        f"\U0001F4B3 {esc(f'{miles:,}')} {esc(program_label)} \\+ \\${esc(f'{taxes_usd:,.0f}')}"
        f"  →  {esc(verdict.headline)}",
    ]
    if deep_link:
        lines.append(f"\U0001F517 [Book on {esc(program_label)}]({deep_link})")
    return "\n".join(lines)


def format_cash_alert(fare: CashFare, verdict: Verdict, baseline: Baseline) -> str:
    """`baseline` is the PREVIOUS baseline (before the observation in
    `fare`) -- src/cash.py's CashBaselineUpdate.previous -- so the drop %
    shown here reflects what the price was expected to be, not the
    just-updated value that already includes this observation."""
    esc = escape_markdown_v2
    cabin_label = fare.cabin.title()
    drop_pct = (baseline.ema_usd - fare.price_usd) / baseline.ema_usd * 100 if baseline.ema_usd else 0.0

    lines = [
        f"\U0001F4C9 *Cash drop* {esc(cabin_label)} {esc(fare.origin)} → {esc(fare.destination)}",
        f"\U0001F4C5 {esc(fare.date.isoformat())}",
        f"\U0001F4B5 {esc(f'${fare.price_usd:,.0f}')} vs {esc(f'${baseline.ema_usd:,.0f}')} baseline "
        f"\\({esc(f'-{drop_pct:.0f}%')}\\)",
    ]
    if fare.deep_link:
        lines.append(f"\U0001F517 [Book flight]({fare.deep_link})")
    return "\n".join(lines)
