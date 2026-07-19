"""Notifier interface -- the swap point for alert delivery.

send_award_alert/send_cash_alert take the raw domain objects (not a
pre-formatted string) so each backend can render however it needs to --
MarkdownV2 text for Telegram, a structured embed for Discord -- without the
poller knowing or caring which one is active.

send_cash_alert has no `deep_link` kwarg, unlike send_award_alert: CashFare
(unlike AwardAvailability) already carries its own `deep_link` field, so
there's nothing for the caller to supply separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src.digest import DigestResult
from src.providers.cash.base import CashFare
from src.providers.seats_aero import AwardAvailability
from src.state import Baseline
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

    def send_cash_alert(self, fare: CashFare, verdict: Verdict, baseline: Baseline | None) -> None: ...

    def send_digest(self, result: DigestResult) -> None: ...
