"""Notifier interface -- the swap point for alert delivery.

send_award_alert takes the raw domain objects (not a pre-formatted string)
so each backend can render however it needs to -- MarkdownV2 text for
Telegram, a structured embed for Discord -- without the poller knowing or
caring which one is active.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src.providers.seats_aero import AwardAvailability
from src.valuation import Verdict


@dataclass(frozen=True)
class Button:
    text: str
    url: str | None = None
    callback_data: str | None = None


class Notifier(Protocol):
    def send_award_alert(
        self, award: AwardAvailability, verdict: Verdict, trip: dict, *, deep_link: str | None = None
    ) -> None: ...
