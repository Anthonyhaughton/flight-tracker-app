"""Lambda entrypoint: orchestrates the v1.1 award + cash pipeline.

seats.aero cached search -> cabin prefilter -> cash baseline lookup/refresh
(week-bucketed) -> valuation gate (real CPP, once cash is available) ->
dedup -> cap -> Get Trips detail -> exact-date cash confirm (one real
SerpApi call, only for candidates that already cleared everything else) ->
final valuation gate -> notifier alert -> record. There is no live-confirm
step for AWARD space specifically: Live Search is commercial-partner-only
and unavailable on a Pro account, so Get Trips (called only on candidates
that already cleared the valuation gate) is the freshness/detail check
instead. Cash gets its own two-stage version of the same idea: the cheap,
week-bucketed baseline decides who's a candidate; a precise, exact-date
call confirms the finalists, because day-of-week fare variance on
long-haul business routes can be large enough that the week's bucket isn't
accurate enough to be the number that actually gates a real alert.

The SAME cash baseline lookup also drives the second, independent trigger
from deal-valuation: a standalone cash-price-drop alert, unrelated to any
specific award redemption -- see src/cash.py's module docstring for why one
cached baseline serves both jobs. The cash-drop trigger and the FIRST-pass
CPP prefilter both intentionally keep using the week-bucketed baseline,
never the exact-date confirm -- see poll_route's body for why.

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
from src.cash import confirm_exact_date_price, get_or_refresh_baseline
from src.config import RouteConfig, WatchlistConfig, load_watchlist
from src.notify.base import Notifier
from src.notify.discord import DiscordNotifier
from src.notify.telegram import TelegramNotifier
from src.providers.cash.base import CashFareProvider
from src.providers.cash.serpapi import SerpApiClient
from src.providers.seats_aero import (
    SeatsAeroAuthError,
    SeatsAeroClient,
    SeatsAeroRateLimitError,
    parse_trip_taxes_usd,
    select_trip_for_cabin,
)
from src.state import DynamoStateStore, StateStore, award_key, cash_key
from src.valuation import is_cash_price_drop, is_high_value, passes_award_prefilter

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
    max_alerts_per_run) is enforced across the whole run, not per-route.

    alerts_sent is the TOTAL of both alert kinds (award + cash) and is what
    max_alerts_per_run gates -- a route producing many cash-drop bucket
    crossings is exactly the same kind of flood risk as many award
    candidates, so they share one budget. cash_alerts_sent is a subset of
    alerts_sent, tracked separately purely so the summary log can show the
    award/cash split."""

    candidates_evaluated: int = 0
    alerts_sent: int = 0
    cash_alerts_sent: int = 0
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
    cash_provider: CashFareProvider | None = None,
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

            # Cheap, pure-function check before spending a (cheap but not
            # free) baseline lookup on an award we'd reject anyway -- Cached
            # Search is already scoped to route.cabins, so this rarely
            # actually filters anything out in practice, but it's a real
            # skip when it does.
            if not passes_award_prefilter(award, wanted_cabins):
                stats.skipped_other += 1
                continue

            comparable_cash_usd = None
            cash_update = None
            if cash_provider is not None:
                try:
                    cash_update = get_or_refresh_baseline(
                        state, cash_provider,
                        origin=award.origin, destination=award.destination, cabin=award.cabin,
                        date=award.date, max_age_minutes=config.schedule.cash_baseline_minutes,
                    )
                except Exception:
                    # A cash-provider hiccup (auth, rate limit, network) must
                    # not crash the whole poll run -- seats.aero award data
                    # is still fully usable without a cash comparison, same
                    # as v1.0's no-cash-provider behavior. See
                    # .claude/skills/flight-cash-price-monitor.
                    logger.warning(
                        "cash baseline lookup failed for %s->%s (%s) on %s, continuing without cash data",
                        award.origin, award.destination, award.cabin, award.date, exc_info=True,
                    )
                else:
                    if cash_update.current_fare is not None:
                        comparable_cash_usd = cash_update.current_fare.price_usd
                    elif cash_update.baseline is not None:
                        comparable_cash_usd = cash_update.baseline.ema_usd

            # Cached Search already carries real per-cabin taxes (award.taxes_usd,
            # None when the program doesn't report them) -- no more 0.0 placeholder.
            verdict = is_high_value(
                award, config.awards, wanted_cabins, comparable_cash_usd=comparable_cash_usd, taxes_usd=award.taxes_usd
            )

            # --- independent cash-drop trigger, piggybacking on the SAME
            # baseline refresh above (not a separate lookup) -- fires
            # regardless of whether the award itself clears the valuation
            # gate, since a cash price drop is meaningful on its own. Never
            # fires on a route's first-ever observation (previous is None).
            if (
                cash_update is not None
                and cash_update.refreshed
                and cash_update.previous is not None
                and cash_update.current_fare is not None
            ):
                cash_verdict = is_cash_price_drop(cash_update.current_fare.price_usd, cash_update.previous, config.cash)
                if cash_verdict.fire:
                    c_key = cash_key(cash_update.current_fare)
                    if state.already_alerted(c_key):
                        stats.skipped_duplicate += 1
                    elif max_alerts_per_run is not None and stats.alerts_sent >= max_alerts_per_run:
                        stats.skipped_capped += 1
                        logger.info(
                            "cash drop %s matched but capped (max_alerts_per_run=%d reached this run), skipping send",
                            c_key, max_alerts_per_run,
                        )
                    else:
                        notifier.send_cash_alert(cash_update.current_fare, cash_verdict, cash_update.previous)
                        state.record_alert(c_key, ttl_seconds=config.alerts.dedup_ttl_days * 86400)
                        stats.alerts_sent += 1
                        stats.cash_alerts_sent += 1

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

            # If the first-pass gate used real cash data at all, it was the
            # week-bucketed baseline -- accurate enough to decide who's a
            # candidate, but day-of-week price variance on long-haul
            # business fares can be large enough that it isn't trustworthy
            # as the number that actually gates a real alert. Spend ONE
            # additional real SerpApi call for this award's EXACT date to
            # confirm, mirroring why Get Trips itself is only called this
            # late: only candidates that already cleared dedup/cap/Get Trips
            # reach here, so this is bounded by the same small numbers, not
            # spent on every candidate. comparable_cash_usd is None here
            # whenever no real cash data was available at all (no provider,
            # or a provider hiccup earlier) -- nothing to confirm in that
            # case, same v1.0-style prefilter-only pass as before.
            if comparable_cash_usd is not None:
                try:
                    confirmed_cash_usd = confirm_exact_date_price(
                        cash_provider, origin=award.origin, destination=award.destination,
                        cabin=award.cabin, date=award.date,
                    )
                except Exception:
                    logger.warning(
                        "exact-date cash confirm failed for %s, skipping (can't verify real CPP)",
                        award.availability_id, exc_info=True,
                    )
                    confirmed_cash_usd = None

                if confirmed_cash_usd is None:
                    logger.info(
                        "%s: no exact-date cash price to confirm the weekly-bucketed estimate, skipping",
                        award.availability_id,
                    )
                    stats.skipped_other += 1
                    continue

                comparable_cash_usd = confirmed_cash_usd

            # Re-run the gate with Get Trips' taxes AND (when cash data was
            # used at all) the exact-date-confirmed cash price above, not
            # the weekly-bucketed one. Cached Search already gave us real
            # taxes earlier (when the program reports them), so this isn't
            # the first place real numbers appear -- it's a confirmation
            # against the more authoritative figures, which can differ
            # (fresher crawl / stale bucket). Skip rather than alert on a
            # stale verdict.
            real_verdict = is_high_value(
                award, config.awards, wanted_cabins,
                comparable_cash_usd=comparable_cash_usd, taxes_usd=parse_trip_taxes_usd(trip),
            )
            if not real_verdict.fire:
                logger.info("%s no longer clears the gate with real numbers, skipping", award.availability_id)
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
    cash_provider: CashFareProvider | None = None,
    state: StateStore | None = None,
    notifier: Notifier | None = None,
    heartbeat: Heartbeat | None = None,
) -> int:
    config = config or load_watchlist()
    owns_seats_client = seats_client is None
    owns_cash_provider = cash_provider is None
    owns_notifier = notifier is None
    seats_client = seats_client or SeatsAeroClient(secrets.get_seats_aero_api_key())
    cash_provider = cash_provider or SerpApiClient(secrets.get_serpapi_key())
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
                cash_provider=cash_provider,
                max_alerts_per_run=config.alerts.max_alerts_per_run,
                stats=stats,
            )
    except SeatsAeroAuthError:
        logger.error("seats.aero auth failed; not retrying within this run")
        raise
    finally:
        if owns_seats_client:
            seats_client.close()
        if owns_cash_provider:
            cash_provider.close()
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
        "poll complete: %d candidate(s) evaluated, %d alert(s) sent (%d cash), %d skipped as duplicate, "
        "%d skipped (cap reached)",
        stats.candidates_evaluated, stats.alerts_sent, stats.cash_alerts_sent,
        stats.skipped_duplicate, stats.skipped_capped,
    )
    return stats.alerts_sent


def lambda_handler(event, context):
    alerts_sent = run()
    return {"statusCode": 200, "alertsSent": alerts_sent}
