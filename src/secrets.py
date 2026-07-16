"""Runtime secret loading.

Every secret comes from an environment variable: locally via a gitignored
`.env` (see .env.example), in Lambda via SSM Parameter Store values injected
as environment variables by the deploy tooling (see infra/secrets.tf). Never
hardcode a key, and never silently fall back to a fake/default value — a
missing secret must fail loud and name exactly what's missing and where to
get it.
"""

from __future__ import annotations

import os


class MissingSecretError(RuntimeError):
    def __init__(self, var_name: str, purpose: str, how_to_get_it: str):
        self.var_name = var_name
        super().__init__(
            f"Missing required environment variable '{var_name}' ({purpose}). "
            f"Get it here: {how_to_get_it}. Set it locally in .env (see "
            f".env.example), or in deployment as an SSM Parameter Store value."
        )


def _require(var_name: str, purpose: str, how_to_get_it: str) -> str:
    value = os.environ.get(var_name)
    if not value:
        raise MissingSecretError(var_name, purpose, how_to_get_it)
    return value


def get_seats_aero_api_key() -> str:
    return _require(
        "SEATS_AERO_API_KEY",
        "seats.aero Pro Partner API key, used to query award availability",
        "https://seats.aero/ -> account settings, after subscribing to a Pro plan with Partner API access",
    )


def get_telegram_bot_token() -> str:
    return _require(
        "TELEGRAM_BOT_TOKEN",
        "Telegram bot token used to send deal alerts",
        "message @BotFather on Telegram and run /newbot",
    )


def get_telegram_chat_id() -> str:
    return _require(
        "TELEGRAM_CHAT_ID",
        "Telegram chat id that alerts get sent to",
        "message your bot once, then call https://api.telegram.org/bot<token>/getUpdates to find your chat id",
    )


def get_serpapi_key() -> str:
    return _require(
        "SERPAPI_KEY",
        "SerpApi key for Google Flights cash fare lookups (not used until v1.1)",
        "https://serpapi.com/ -> dashboard -> API key",
    )
