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
import os
from dataclasses import dataclass
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
    select_trip_for_cabin,
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


@dataclass
class PollStats:
    """Accumulated across every route/origin in a single run() invocation --
    passed by reference into poll_route() so the cap (config.alerts.
    max_alerts_per_run) is enforced across the whole run, not per-route."""

    candidates_evaluated: int = 0
    alerts_sent: int = 0
    skipped_duplicate: int = 0
    skipped_capped: int = 0
    skipped_other: int = 0


def _require_env(var_name: str, purpose: str) -> str:
    value = os.environ.get(var_name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable '{var_name}' ({purpose}). "
            "Expected to be set by infra/lambda.tf's environment.variables block, "
            "from the real Terraform-created table's .name -- this shouldn't be "
            "missing in a real deployment."
        )
    return value


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
    *,
    max_alerts_per_run: int | None = None,
    stats: PollStats | None = None,
) -> PollStats:
    wanted_cabins = set(route.cabins)
    start, end = route.date_window.to_dates()
    stats = stats if stats is not None else PollStats()

    for origin in origins:
        try:
            hits = client.cached_search(origin, route.destinations, start, end, route.cabins)
        except SeatsAeroRateLimitError as exc:
            logger.warning("rate limited on %s from %s, stopping this run: %s", route.name, origin, exc)
            break

        for award in hits:
            stats.candidates_evaluated += 1

            # Cached Search already carries real per-cabin taxes (award.taxes_usd,
            # None when the program doesn't report them) -- no more 0.0 placeholder.
            verdict = is_high_value(award, config.awards, wanted_cabins, taxes_usd=award.taxes_usd)
            if not verdict.fire:
                stats.skipped_other += 1
                continue

            key = award_key(award)
            if state.already_alerted(key):
                stats.skipped_duplicate += 1
                continue

            # Cap check happens here -- after dedup (so it's independent of
            # dedup, per the design goal: dedup filters repeats across runs,
            # this caps NEW deals within a single run), before Get Trips (so
            # a capped candidate doesn't burn seats.aero quota on a call we
            # already know won't lead to a send). Log it, don't drop it
            # silently -- it genuinely matched, it just lost the race for
            # this run's budget.
            if max_alerts_per_run is not None and stats.alerts_sent >= max_alerts_per_run:
                stats.skipped_capped += 1
                logger.info(
                    "%s matched but capped (max_alerts_per_run=%d reached this run), skipping send",
                    award.availability_id, max_alerts_per_run,
                )
                continue

            trips = client.get_trips(award.availability_id)
            if not trips:
                logger.info("no trip detail for %s, skipping (space likely gone)", award.availability_id)
                stats.skipped_other += 1
                continue

            # Get Trips returns itineraries across ALL cabins on this
            # availability, not just the one we matched -- trips[0] is not
            # guaranteed to be (and in practice often isn't) award.cabin.
            trip = select_trip_for_cabin(trips, award.cabin)
            if trip is None:
                logger.info(
                    "no %s-cabin trip among %d Get Trips result(s) for %s, skipping",
                    award.cabin, len(trips), award.availability_id,
                )
                stats.skipped_other += 1
                continue

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
                stats.skipped_other += 1
                continue

            notifier.send_award_alert(award, real_verdict, trip)
            state.record_alert(key, ttl_seconds=config.alerts.dedup_ttl_days * 86400)
            stats.alerts_sent += 1

    return stats


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
    # Table names must come from the real Terraform-created resources (see
    # infra/lambda.tf's environment.variables block), never a hardcoded
    # string here -- IAM only grants access to the tables Terraform actually
    # created (infra/iam.tf), so a stale/guessed name here 403s instead of
    # 404ing, which is exactly what happened when this was hardcoded.
    state = state or DynamoStateStore(
        alerts_table=_require_env("ALERTS_TABLE_NAME", "DynamoDB alerts/dedup table name"),
        baselines_table=_require_env("BASELINES_TABLE_NAME", "DynamoDB cash-baselines table name"),
    )
    notifier = notifier or _build_notifier(config.notifier)
    heartbeat = heartbeat or CloudWatchHeartbeat()

    stats = PollStats()
    try:
        for route in config.active_routes():
            poll_route(
                seats_client,
                config.origins,
                route,
                config,
                state,
                notifier,
                max_alerts_per_run=config.alerts.max_alerts_per_run,
                stats=stats,
            )
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
    # heartbeat rather than a silent, misleading "no alerts." Logged either
    # way (0 alerts included) so the logs always make it obvious whether the
    # cap -- as opposed to dedup or just "no deals" -- was the limiting
    # factor on a given run.
    heartbeat.emit()
    logger.info(
        "poll complete: %d candidate(s) evaluated, %d alert(s) sent, %d skipped as duplicate, "
        "%d skipped (cap reached)",
        stats.candidates_evaluated, stats.alerts_sent, stats.skipped_duplicate, stats.skipped_capped,
    )
    return stats.alerts_sent


def lambda_handler(event, context):
    alerts_sent = run()
    return {"statusCode": 200, "alertsSent": alerts_sent}
