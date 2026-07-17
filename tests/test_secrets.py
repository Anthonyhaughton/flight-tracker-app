from __future__ import annotations

from unittest import mock

import pytest

from src import secrets


@pytest.fixture(autouse=True)
def _clear_ssm_cache():
    secrets._ssm_cache.clear()
    yield
    secrets._ssm_cache.clear()


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


# --- Lambda path: resolved via SSM at cold start, not os.environ ---


def test_lambda_path_resolves_via_ssm(monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "flight-tracker-app-poller")
    monkeypatch.setenv("SEATS_AERO_API_KEY_SSM_PARAM", "/flight-tracker-app/seats_aero_api_key")
    monkeypatch.delenv("SEATS_AERO_API_KEY", raising=False)  # must NOT be read directly in Lambda mode

    fake_client = mock.Mock()
    fake_client.get_parameter.return_value = {"Parameter": {"Value": "real-key-from-ssm"}}
    monkeypatch.setattr(secrets.boto3, "client", lambda service: fake_client)

    result = secrets.get_seats_aero_api_key()

    assert result == "real-key-from-ssm"
    fake_client.get_parameter.assert_called_once_with(
        Name="/flight-tracker-app/seats_aero_api_key", WithDecryption=True
    )


def test_lambda_path_caches_ssm_result_across_calls(monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "flight-tracker-app-poller")
    monkeypatch.setenv("SEATS_AERO_API_KEY_SSM_PARAM", "/flight-tracker-app/seats_aero_api_key")

    fake_client = mock.Mock()
    fake_client.get_parameter.return_value = {"Parameter": {"Value": "real-key-from-ssm"}}
    monkeypatch.setattr(secrets.boto3, "client", lambda service: fake_client)

    secrets.get_seats_aero_api_key()
    secrets.get_seats_aero_api_key()

    # cached after the first cold-start resolution -- a warm invocation
    # must not re-hit SSM on every secret access.
    assert fake_client.get_parameter.call_count == 1


def test_lambda_path_raises_when_ssm_param_name_env_var_missing(monkeypatch):
    # infra/lambda.tf should always set this -- but if it's ever missing,
    # fail loud and name the exact env var, not a generic AWS error.
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "flight-tracker-app-poller")
    monkeypatch.delenv("SEATS_AERO_API_KEY_SSM_PARAM", raising=False)

    with pytest.raises(secrets.MissingSecretError) as exc_info:
        secrets.get_seats_aero_api_key()
    assert "SEATS_AERO_API_KEY_SSM_PARAM" in str(exc_info.value)


def test_local_path_ignores_ssm_param_env_var_outside_lambda(monkeypatch):
    # local/dev behavior must be untouched by this change: even if an
    # _SSM_PARAM var is somehow set, os.environ is still read directly
    # outside Lambda.
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.setenv("SEATS_AERO_API_KEY", "local-dev-key")
    monkeypatch.setenv("SEATS_AERO_API_KEY_SSM_PARAM", "/should/be/ignored")

    assert secrets.get_seats_aero_api_key() == "local-dev-key"


def test_lambda_path_resolves_discord_webhook_url(monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "flight-tracker-app-poller")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL_SSM_PARAM", "/flight-tracker-app/discord_webhook_url")

    fake_client = mock.Mock()
    fake_client.get_parameter.return_value = {"Parameter": {"Value": "https://discord.com/api/webhooks/x/y"}}
    monkeypatch.setattr(secrets.boto3, "client", lambda service: fake_client)

    assert secrets.get_discord_webhook_url() == "https://discord.com/api/webhooks/x/y"
