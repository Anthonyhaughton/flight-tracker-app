from __future__ import annotations

import pytest

from src import secrets


def test_missing_seats_aero_key_raises_actionable_error(monkeypatch):
    monkeypatch.delenv("SEATS_AERO_API_KEY", raising=False)
    with pytest.raises(secrets.MissingSecretError) as exc_info:
        secrets.get_seats_aero_api_key()
    message = str(exc_info.value)
    assert "SEATS_AERO_API_KEY" in message
    assert "seats.aero" in message


def test_missing_telegram_token_raises_actionable_error(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(secrets.MissingSecretError) as exc_info:
        secrets.get_telegram_bot_token()
    assert "TELEGRAM_BOT_TOKEN" in str(exc_info.value)


def test_present_secret_is_returned(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    assert secrets.get_telegram_chat_id() == "12345"


def test_empty_string_secret_still_raises(monkeypatch):
    monkeypatch.setenv("SERPAPI_KEY", "")
    with pytest.raises(secrets.MissingSecretError):
        secrets.get_serpapi_key()
