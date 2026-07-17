"""Runtime secret loading.

Locally (or anywhere AWS_LAMBDA_FUNCTION_NAME isn't set): every secret comes
straight from an environment variable, via a gitignored `.env` (see
.env.example). Never hardcode a key, never silently fall back to a fake
value -- a missing secret must fail loud and name exactly what's missing and
where to get it.

In Lambda: Terraform never injects secret *values* as environment variables
-- that would land decrypted secrets in Terraform state, plan output, and
the Lambda console's environment-variables view. Instead each secret's SSM
Parameter Store *name* (not sensitive -- just a path like
"/flight-tracker-app/seats_aero_api_key") is injected as `{VAR}_SSM_PARAM`
(see infra/lambda.tf), and this module resolves the real value via boto3
ssm.get_parameter(WithDecryption=True) at cold start, caching it in-process
for the lifetime of the container so a warm invocation never re-hits SSM.
"""

from __future__ import annotations

import os

import boto3


class MissingSecretError(RuntimeError):
    def __init__(self, var_name: str, purpose: str, how_to_get_it: str):
        self.var_name = var_name
        super().__init__(
            f"Missing required environment variable '{var_name}' ({purpose}). "
            f"Get it here: {how_to_get_it}. Set it locally in .env (see "
            f".env.example), or in deployment as an SSM Parameter Store value."
        )


# Cold-start cache, keyed by SSM parameter name -- module-level so it
# persists across warm invocations within the same Lambda container. A
# plain dict (not functools.lru_cache) so tests can reset it deterministically.
_ssm_cache: dict[str, str] = {}


def _is_lambda() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def _resolve_from_ssm(param_name: str) -> str:
    if param_name not in _ssm_cache:
        client = boto3.client("ssm")
        response = client.get_parameter(Name=param_name, WithDecryption=True)
        _ssm_cache[param_name] = response["Parameter"]["Value"]
    return _ssm_cache[param_name]


def _require(var_name: str, purpose: str, how_to_get_it: str) -> str:
    if _is_lambda():
        param_name_var = f"{var_name}_SSM_PARAM"
        param_name = os.environ.get(param_name_var)
        if not param_name:
            raise MissingSecretError(
                param_name_var,
                f"SSM Parameter Store path for {purpose}",
                "set by Terraform (infra/lambda.tf) -- this shouldn't be missing in a real deployment",
            )
        value = _resolve_from_ssm(param_name)
        if not value:
            raise MissingSecretError(var_name, purpose, how_to_get_it)
        return value

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


def get_discord_webhook_url() -> str:
    return _require(
        "DISCORD_WEBHOOK_URL",
        "Discord webhook URL used to send deal alerts (default v1.0 notifier)",
        "Server Settings -> Integrations -> Webhooks -> New Webhook -> Copy Webhook URL",
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
        "SerpApi key for Google Flights cash fare lookups",
        "https://serpapi.com/ -> dashboard -> API key",
    )
