"""Lambda entrypoint: orchestrates the v1.0 award-only pipeline.

seats.aero cached search -> cabin prefilter -> valuation gate -> dedup ->
Get Trips detail -> notifier alert -> record. There is no live-confirm step:
Live Search is commercial-partner-only and unavailable on a Pro account, so
Get Trips (called only on candidates that already cleared the valuation
gate) is the freshness/detail check instead.

Notifier is selected via watchlist.yaml's `notifier` key (discord by
default; telegram is a swappable alternate impl -- see src/notify/).

Safe to retry: alerts are recorded only after a successful send, and dedup
means a retried run re-evaluates rather than double-alerting.
"""

from __future__ import annotations

import logging
from typing import Protocol

import boto3

from src import secrets
from src.config import RouteConfig, WatchlistConfig, load_watchlist
from src.notify.base import Notifier
from src.notify.discord import DiscordNotifier
from src.notify.telegram import TelegramNotifier
from src.providers.seats_aero import (
    SeatsAeroAuthError,
    SeatsAeroClient,
    SeatsAeroRateLimitError,
    parse_trip_taxes_usd,
)
from src.state import DynamoStateStore, StateStore, award_key
from src.valuation import is_high_value

logger = logging.getLogger("poller")

# Matches infra/monitoring.tf's alarm namespace/metric -- keep these in sync
# if the Terraform defaults change.
HEARTBEAT_NAMESPACE = "flight-deal-agent/Heartbeat"
HEARTBEAT_METRIC = "PollSucceeded"


class Heartbeat(Protocol):
    def emit(self) -> None: ...


def _build_notifier(notifier_name: str) -> Notifier:
    if notifier_name == "discord":
        return DiscordNotifier(secrets.get_discord_webhook_url())
    if notifier_name == "telegram":
        return TelegramNotifier(secrets.get_telegram_bot_token(), secrets.get_telegram_chat_id())
    raise ValueError(f"Unknown notifier {notifier_name!r} in watchlist.yaml (expected 'discord' or 'telegram')")


class CloudWatchHeartbeat:
    """Dead-man's-switch: emit a metric on every successful run so the
    CloudWatch alarm can tell 'no deals' apart from 'the poller is dead'."""

    def __init__(self, *, namespace: str = HEARTBEAT_NAMESPACE, metric_name: str = HEARTBEAT_METRIC, client=None):
        self._namespace = namespace
        self._metric_name = metric_name
        self._client = client or boto3.client("cloudwatch")

    def emit(self) -> None:
        self._client.put_metric_data(
            Namespace=self._namespace,
            MetricData=[{"MetricName": self._metric_name, "Value": 1.0, "Unit": "Count"}],
        )


def poll_route(
    client: SeatsAeroClient,
    origins: list[str],
    route: RouteConfig,
    config: WatchlistConfig,
    state: StateStore,
    notifier: Notifier,
) -> int:
    wanted_cabins = set(route.cabins)
    start, end = route.date_window.to_dates()
    alerts_sent = 0

    for origin in origins:
        try:
            hits = client.cached_search(origin, route.destinations, start, end, route.cabins)
        except SeatsAeroRateLimitError as exc:
            logger.warning("rate limited on %s from %s, stopping this run: %s", route.name, origin, exc)
            break

        for award in hits:
            # Cached Search already carries real per-cabin taxes (award.taxes_usd,
            # None when the program doesn't report them) -- no more 0.0 placeholder.
            verdict = is_high_value(award, config.awards, wanted_cabins, taxes_usd=award.taxes_usd)
            if not verdict.fire:
                continue

            key = award_key(award)
            if state.already_alerted(key):
                continue

            trips = client.get_trips(award.availability_id)
            if not trips:
                logger.info("no trip detail for %s, skipping (space likely gone)", award.availability_id)
                continue
            trip = trips[0]

            # Re-run the gate with Get Trips' taxes. Cached Search already gave
            # us real taxes above (when the program reports them), so this
            # isn't the first place real numbers appear -- it's a confirmation
            # against the more authoritative Get Trips figure, which can
            # differ (fresher crawl, or Cached Search's number was stale).
            # In v1.0 (no cash provider wired in, so comparable_cash_usd is
            # always None) this is a no-op; once v1.1 adds real cash
            # comparison, it's what catches a deal that only cleared the gate
            # on Cached Search's now-stale taxes. Skip rather than alert on a
            # stale verdict.
            real_verdict = is_high_value(award, config.awards, wanted_cabins, taxes_usd=parse_trip_taxes_usd(trip))
            if not real_verdict.fire:
                logger.info("%s no longer clears the gate with real taxes, skipping", award.availability_id)
                continue

            notifier.send_award_alert(award, real_verdict, trip)
            state.record_alert(key, ttl_seconds=config.alerts.dedup_ttl_days * 86400)
            alerts_sent += 1

    return alerts_sent


def run(
    config: WatchlistConfig | None = None,
    *,
    seats_client: SeatsAeroClient | None = None,
    state: StateStore | None = None,
    notifier: Notifier | None = None,
    heartbeat: Heartbeat | None = None,
) -> int:
    config = config or load_watchlist()
    owns_seats_client = seats_client is None
    owns_notifier = notifier is None
    seats_client = seats_client or SeatsAeroClient(secrets.get_seats_aero_api_key())
    state = state or DynamoStateStore(alerts_table="flight-deal-alerts", baselines_table="flight-deal-baselines")
    notifier = notifier or _build_notifier(config.notifier)
    heartbeat = heartbeat or CloudWatchHeartbeat()

    total_alerts = 0
    try:
        for route in config.active_routes():
            total_alerts += poll_route(seats_client, config.origins, route, config, state, notifier)
    except SeatsAeroAuthError:
        logger.error("seats.aero auth failed; not retrying within this run")
        raise
    finally:
        if owns_seats_client:
            seats_client.close()
        if owns_notifier:
            notifier.close()

    # Only reached on a clean run -- an unhandled exception above (e.g. auth
    # failure) skips this, so a dead/broken poller shows up as a missed
    # heartbeat rather than a silent, misleading "no alerts."
    heartbeat.emit()
    logger.info("poll complete: %d alerts sent", total_alerts)
    return total_alerts


def lambda_handler(event, context):
    alerts_sent = run()
    return {"statusCode": 200, "alertsSent": alerts_sent}
