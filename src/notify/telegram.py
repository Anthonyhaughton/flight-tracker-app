"""Telegram Bot API notifier: MarkdownV2 formatting/escaping + sending.

Not spamming lives in valuation.py's dedup; this module only formats and
sends (see .claude/skills/telegram-alerting).
"""

from __future__ import annotations

import datetime
import re

import httpx

from src.digest import DigestEntry, DigestResult
from src.notify.base import Button
from src.providers.cash.base import CashFare
from src.providers.seats_aero import AwardAvailability, parse_trip_taxes_usd
from src.state import Baseline
from src.valuation import Verdict, compute_transfer_bonus_effective_miles

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
        self,
        award: AwardAvailability,
        verdict: Verdict,
        trip: dict,
        *,
        deep_link: str | None = None,
        transfer_bonus_pct: float = 0.0,
        group_other_dates: list[datetime.date] | None = None,
    ) -> None:
        self.send(
            format_award_alert(
                award, verdict, trip, deep_link=deep_link, transfer_bonus_pct=transfer_bonus_pct,
                group_other_dates=group_other_dates,
            )
        )

    def send_cash_alert(self, fare: CashFare, verdict: Verdict, baseline: Baseline | None) -> None:
        self.send(format_cash_alert(fare, verdict, baseline))

    def send_digest(self, result: DigestResult) -> None:
        self.send(format_digest_alert(result))

    def close(self) -> None:
        self._client.close()


def _button_to_dict(button: Button) -> dict:
    d: dict = {"text": button.text}
    if button.url:
        d["url"] = button.url
    if button.callback_data:
        d["callback_data"] = button.callback_data
    return d


def format_award_alert(
    award: AwardAvailability,
    verdict: Verdict,
    trip: dict,
    *,
    deep_link: str | None = None,
    transfer_bonus_pct: float = 0.0,
    group_other_dates: list[datetime.date] | None = None,
) -> str:
    """`trip` is one entry from SeatsAeroClient.get_trips(award.availability_id),
    already filtered to award.cabin by select_trip_for_cabin() in poller.py
    (Get Trips returns itineraries across ALL cabins on the availability, so
    trip["Cabin"] can't be trusted blindly). The cabin label uses award.cabin
    directly, not trip's own Cabin field.

    No "saver" in the message: there's no per-item saver flag on the wire --
    saver-equivalence comes entirely from the Cached Search request-time
    filter, so claiming it per-item here would assert something we can't
    verify.

    `transfer_bonus_pct` (0.0 = no active bonus, the common case) adds a
    line showing the informational effective-points cost -- see
    src/valuation.py's compute_transfer_bonus_effective_miles. Computed off
    `miles` below (the Get-Trips-confirmed figure), not award.miles, same
    reasoning as format_award_embed's Discord equivalent.

    `group_other_dates` (empty/None by default) lists every OTHER date in
    this award's (origin, destination, cabin, program, calendar month)
    group that also cleared the first-pass gate but lost to this one -- see
    src/valuation.py's select_group_winners. Adds a line naming the month
    and those specific dates when non-empty; shows nothing when empty."""
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
    if transfer_bonus_pct:
        effective_miles = compute_transfer_bonus_effective_miles(miles, transfer_bonus_pct)
        lines.append(
            f"\U0001F4B1 {esc(f'{transfer_bonus_pct * 100:.0f}%')} transfer bonus active — effective cost "
            f"~{esc(f'{effective_miles:,.0f}')} pts"
        )
    if group_other_dates:
        month_name = award.date.strftime("%B")
        dates_str = ", ".join(d.isoformat() for d in group_other_dates)
        lines.append(
            f"\U0001F4C6 {esc(f'+{len(group_other_dates)}')} other date(s) in {esc(month_name)} also qualify "
            f"\\({esc(dates_str)}\\)"
        )
    if deep_link:
        lines.append(f"\U0001F517 [Book on {esc(program_label)}]({deep_link})")
    return "\n".join(lines)


def format_cash_alert(fare: CashFare, verdict: Verdict, baseline: Baseline | None) -> str:
    """`baseline` is the PREVIOUS baseline (before the observation in
    `fare`) -- src/cash.py's CashBaselineUpdate.previous -- so the drop %
    shown here reflects what the price was expected to be, not the
    just-updated value that already includes this observation.

    `baseline` may be None: the absolute mistake-fare-ceiling trigger (see
    src/valuation.py's is_cash_below_mistake_fare_ceiling) fires regardless
    of history, including on a route's very first-ever observation -- in
    that case there's nothing to compare against, so the baseline/drop line
    is simply omitted."""
    esc = escape_markdown_v2
    cabin_label = fare.cabin.title()

    lines = [
        f"\U0001F4C9 *Cash drop* {esc(cabin_label)} {esc(fare.origin)} → {esc(fare.destination)}",
        f"\U0001F4C5 {esc(fare.date.isoformat())}",
    ]
    if baseline is not None:
        drop_pct = (baseline.ema_usd - fare.price_usd) / baseline.ema_usd * 100 if baseline.ema_usd else 0.0
        lines.append(
            f"\U0001F4B5 {esc(f'${fare.price_usd:,.0f}')} vs {esc(f'${baseline.ema_usd:,.0f}')} baseline "
            f"\\({esc(f'-{drop_pct:.0f}%')}\\)"
        )
    else:
        lines.append(f"\U0001F4B5 {esc(f'${fare.price_usd:,.0f}')} one-way")
    if fare.deep_link:
        lines.append(f"\U0001F517 [Book flight]({fare.deep_link})")
    return "\n".join(lines)


def _format_digest_entry_line(entry: DigestEntry) -> str:
    esc = escape_markdown_v2
    program_label = entry.award.program.replace("_", " ").title()
    match_note = " ⭐" if entry.cleared_real_time_bar else ""
    bonus_note = ""
    if entry.transfer_bonus_pct:
        effective_miles = compute_transfer_bonus_effective_miles(entry.award.miles, entry.transfer_bonus_pct)
        bonus_note = (
            f" \\| {esc(f'{entry.transfer_bonus_pct * 100:.0f}%')} transfer bonus active, "
            f"~{esc(f'{effective_miles:,.0f}')} pts effective"
        )
    return (
        f"• {esc(entry.award.origin)} → {esc(entry.award.destination)} "
        f"{esc(entry.award.date.isoformat())}: {esc(program_label)} {esc(f'{entry.award.miles:,}')} mi "
        f"\\+ \\${esc(f'{entry.taxes_usd:,.0f}')} → \\${esc(f'{entry.comparable_cash_usd:,.0f}')} cash, "
        f"{esc(f'{entry.cpp:.1f}')}cpp, \\${esc(f'{entry.trip_value_usd:,.0f}')} value{match_note}{bonus_note}"
    )


def format_digest_alert(result: DigestResult) -> str:
    """Single MarkdownV2 message, two sections (Top Cash Value / Top CPP) --
    NOT ten separate alert-style messages. Mirrors format_digest_embeds'
    Discord content: an honest "no availability" message when both rankings
    are empty, and a "nothing cleared the real-time bar" line naming the
    closest miss when the digest has entries but none would have fired a
    real alert. See deal-valuation's digest spec."""
    esc = escape_markdown_v2

    if not result.cash_rank and not result.cpp_rank:
        return (
            f"\U0001F4CA *Weekly Deal Digest*\n"
            f"No award availability found this week \\({esc(result.candidates_evaluated)} "
            f"candidate\\(s\\) evaluated across all active routes\\)\\."
        )

    lines = [
        "\U0001F4CA *Weekly Deal Digest*",
        "",
        "\U0001F4B5 *Top Cash Value*",
        *[_format_digest_entry_line(e) for e in result.cash_rank],
        "",
        "\U0001F3AF *Top CPP*",
        *[_format_digest_entry_line(e) for e in result.cpp_rank],
    ]

    cleared_any = any(e.cleared_real_time_bar for e in (*result.cash_rank, *result.cpp_rank))
    if not cleared_any:
        closest = result.cpp_rank[0]
        program_label = closest.award.program.replace("_", " ").title()
        lines += [
            "",
            f"Nothing cleared the real\\-time bar this week — closest was {esc(f'{closest.cpp:.1f}')}cpp "
            f"on {esc(program_label)} \\(floor {esc(f'{closest.real_time_cpp_floor:.1f}')}cpp\\)\\.",
        ]

    return "\n".join(lines)
