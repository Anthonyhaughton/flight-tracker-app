"""Notifier interface -- the swap point for alert delivery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Button:
    text: str
    url: str | None = None
    callback_data: str | None = None


class Notifier(Protocol):
    def send(self, message: str, buttons: list[Button] | None = None) -> None: ...
