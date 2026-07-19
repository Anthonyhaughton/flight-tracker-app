"""Lambda entrypoint: orchestrates the v1.1 award + cash pipeline.

seats.aero cached search -> cabin prefilter -> cash baseline lookup/refresh
(week-bucketed) -> first-pass valuation gate (real CPP, once cash is
available) -> group-winner selection (see below) -> dedup -> cap -> Get
Trips detail -> exact-date cash confirm (one real SerpApi call, only for
the single winning candidate per group) -> final valuation gate ->
notifier alert -> record. There is no live-confirm step for AWARD space
specifically: Live Search is commercial-partner-only and unavailable on a
Pro account, so Get Trips (called only on a group winner that already
cleared the first-pass valuation gate) is the freshness/detail check
instead. Cash gets its own two-stage version of the same idea: the cheap,
week-bucketed baseline decides who's a candidate; a precise, exact-date
call confirms the finalist, because day-of-week fare variance on
long-haul business routes can be large enough that the week's bucket isn't
accurate enough to be the number that actually gates a real alert.

**Group-winner selection** (src/valuation.py's select_group_winners,
.claude/skills/deal-valuation's winner-selection spec): before any
first-pass-gate-passing candidate reaches dedup/cap/Get-Trips/confirm/
notify, poll_route() groups this route's candidates by (origin,
destination, cabin, program, calendar month) and keeps only the single
highest-cpp candidate per group -- every other candidate in that group is
dropped entirely (not sent, not counted as capped, not confirmed), so
near-duplicate dates of one deal (e.g. the same business saver open on 4
dates in the same month) never each independently spend a real Get Trips/
exact-confirm call or compete for the per-run alert cap. Scoped to
calendar MONTH, not the whole date window -- dates a month or more apart
are genuinely different trip options and each get their own shot. The sent
alert names the other qualifying dates it beat (Notifier.send_award_alert's
group_other_dates), so that flexibility isn't silently hidden, just not
spammed as separate messages. The SAME grouping is applied by
src/digest.py before its own top-5 ranking, for the identical reason.

The SAME cash baseline lookup also drives the second, independent trigger
from deal-valuation: a standalone cash-price-drop alert, unrelated to any
specific award redemption -- see src/cash.py's module docstring for why one
cached baseline serves both jobs. The cash-drop trigger and the FIRST-pass
CPP prefilter both intentionally keep using the week-bucketed baseline,
never the exact-date confirm -- see poll_route's body for why.

Notifier is selected via watchlist.yaml's `notifier` key (discord by
default; telegram is a swappable alternate impl -- see src/notify/).

`lambda_handler` also dispatches to `run_digest()` -- a separate, weekly
snapshot-ranking path (see src/digest.py) invoked via a distinct event
payload (`{"mode": "digest"}`) on a second EventBridge schedule hitting this
SAME Lambda -- when it's given that mode. Any other event preserves this
module's pre-digest real-time behavior exactly.

One more config-driven gate, applied per candidate: `eligible_programs`
(top-level watchlist.yaml key, an early prefilter alongside the cabin check
-- a program not in the set is skipped before any cash lookup or Get Trips
call). See src/valuation.py.

A THIRD free prefilter, alongside those two: a business/first candidate
whose miles cost exceeds `awards.premium_cabin_max_multiplier` times
economy's miles cost on the SAME seats.aero record (both cabins' costs are
present on one Cached Search row -- confirmed via a real live call, no
second call needed, see AwardAvailability.economy_miles) is rejected before
a cash lookup too. Both active routes now watch economy AND business/first
(re-added 2026-07), so this exists specifically to stop an obviously-bad
premium-cabin redemption from spending real SerpApi budget just to be
rejected downstream anyway.

Separately, `awards.transfer_bonus_pct` (per-program, manually maintained,
default 0) is purely informational: when nonzero for the alerting award's
program, the real-time alert and the weekly digest both show an
"effective cost" annotation (miles / (1 + bonus)) alongside the real miles
number -- see src/valuation.py's compute_transfer_bonus_effective_miles.
It never changes any gating/threshold decision.

A candidate with no resolved cash price -- a baseline lookup failure, a
provider error, or a route with genuinely no cash data available -- always
skips, on every route, unconditionally (src/valuation.py's is_high_value).
An earlier version fell back to firing on cabin-match alone when cash data
was unavailable; that direction was retired 2026-07 as a real safety issue:
it meant the cash pipeline's failure mode was MORE alerts, not fewer, on a
system whose top priority is avoiding alert fatigue.

Origins are resolved per route: `route.origins` overrides the top-level
`origins` list when set, otherwise the top-level list applies.

Safe to retry: alerts are recorded only after a successful send, and dedup
means a retried run re-evaluates rather than double-alerting.

`classify_candidate()` and `finish_award_candidate()` are the shared
per-candidate decision pipeline, split into two phases specifically so
group-winner selection can happen in between them: `classify_candidate()`
(phase 1 -- cash triggers + first-pass gate, immediate, no deferral) is
called for EVERY candidate; `select_group_winners()` then decides which
fired candidates proceed; `finish_award_candidate()` (phase 2 -- dedup ->
cap -> `fetch_trip` -> final gate -> notify + record) is called ONLY for
each group's winner. poll_route() (below) and scripts/dry_run.py both call
these SAME functions for every candidate; neither reimplements them. The
only thing that varies per caller is `fetch_trip`: fetching Get Trips
detail and confirming the exact-date cash price are genuinely different
per caller (production propagates a Get Trips failure loudly and swallows
a confirm failure broadly; dry_run.py aborts loudly on auth/quota for
either and treats a timeout as a skip of just that one candidate -- see
each file's own `fetch_trip` implementation) -- that I/O + error-handling
policy stays with the caller. Everything else -- the actual decision
logic, including grouping -- lives in these shared functions exactly once.
"""

from __future__ import annotations

import datetime
import logging
import os
from dataclasses import dataclass
from typing import Callable, Protocol

import boto3

from src import secrets
from src.cash import CashBaselineUpdate, confirm_exact_date_price, get_or_refresh_baseline
from src.config import RouteConfig, WatchlistConfig, load_watchlist
from src.digest import DigestResult, build_weekly_digest
from src.notify.base import Notifier
from src.notify.discord import DiscordNotifier
from src.notify.telegram import TelegramNotifier
from src.providers.cash.base import CashFare, CashFareProvider
from src.providers.cash.serpapi import SerpApiClient
from src.providers.seats_aero import (
    AwardAvailability,
    SeatsAeroAuthError,
    SeatsAeroClient,
    SeatsAeroRateLimitError,
    parse_trip_taxes_usd,
    select_trip_for_cabin,
)
from src.state import DynamoStateStore, StateStore, award_key, cash_key
from src.valuation import (
    Verdict,
    compute_effective_cpp,
    is_cash_below_mistake_fare_ceiling,
    is_cash_price_drop,
    is_high_value,
    passes_award_prefilter,
    select_group_winners,
)

logger = logging.getLogger("poller")

# Matches infra/monitoring.tf's local.heartbeat_namespace ("${var.project_name}/Heartbeat")
# and infra/iam.tf's Heartbeat statement condition -- keep all three in sync if the
# project name or Terraform defaults change. (This constant was a stale pre-rename
# string, "flight-deal-agent/Heartbeat", from 2026-07 until this fix -- see
# avoiding-duplicate-implementations's "stale rename strings" section for the incident.)
HEARTBEAT_NAMESPACE = "flight-tracker-app/Heartbeat"
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
    award/cash split.

    skipped_grouped_out is DISTINCT from skipped_capped and skipped_duplicate:
    it counts a candidate that cleared the first-pass gate but lost to a
    higher-cpp candidate in the SAME (origin, destination, cabin, program,
    calendar month) group -- see select_group_winners() and finish_award_
    candidate()'s callers. Never counted as capped (it never even reached
    the cap check) and never as a duplicate (dedup never ran on it either) --
    a distinct bucket so the run summary can tell "genuinely too many new
    deals this run" apart from "multiple dates of the same deal, only the
    best one was worth spending a real Get Trips/confirm call on"."""

    candidates_evaluated: int = 0
    alerts_sent: int = 0
    cash_alerts_sent: int = 0
    skipped_duplicate: int = 0
    skipped_capped: int = 0
    skipped_other: int = 0
    skipped_grouped_out: int = 0


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


@dataclass(frozen=True)
class TripFetchResult:
    """Result of fetching Get Trips detail and (unconditionally, once
    reached -- see finish_award_candidate's docstring) confirming the
    exact-date cash price for a candidate that already cleared the
    first-pass gate, group-winner selection, dedup, and the cap. Produced
    by a caller-supplied `fetch_trip` callback so each caller keeps its OWN
    error-handling policy for those two I/O calls (see poll_route()'s and
    scripts/dry_run.py's own implementations) -- exceptions that should
    abort the whole run (e.g. auth/quota failures) must be allowed to
    propagate OUT of the callback uncaught; this type is only for the
    "give up on evaluating this one candidate further" outcome.

    `skip_reason`, when set, is used as-is in place of finish_award_
    candidate's own generic reason text -- lets a caller's callback (e.g.
    dry_run.py's timeout handling) supply a more specific explanation
    without finish_award_candidate needing to know about every possible I/O
    failure mode a given caller might want to distinguish.
    """

    trips: list[dict] | None
    confirmed_cash_usd: float | None
    skip_reason: str | None = None


@dataclass(frozen=True)
class CashOutcome:
    """What happened with the independent cash-drop/mistake-fare-ceiling
    trigger for one candidate -- entirely separate from the award outcome
    below, since a cash alert can fire regardless of whether the award
    itself clears the gate. outcome is one of "not_triggered" (neither cash
    trigger fired, or there was nothing to evaluate), "sent", "duplicate",
    "capped"."""

    outcome: str
    fare: CashFare | None = None
    verdict: Verdict | None = None
    key: str | None = None
    # True when classify_candidate (or the caller's own fetch_trip callback,
    # for a "capped" cash outcome this never applies to) already logged this
    # outcome itself -- lets a caller with its own verbose logging (e.g.
    # scripts/dry_run.py) skip re-logging what's already been said, without
    # needing to know which specific outcomes those are.
    already_logged: bool = False


@dataclass(frozen=True)
class AwardOutcome:
    """What happened with the award redemption itself for one candidate.
    outcome is one of "sent", "skipped". `reason` is always a complete,
    human-readable explanation -- callers that want per-candidate logging
    (scripts/dry_run.py) can log it directly rather than reconstructing
    their own message per skip reason.

    `already_logged` is True when finish_award_candidate (or the caller's
    own fetch_trip callback) already emitted a log line for this outcome --
    lets a caller with its own verbose logging skip re-logging it, without
    needing to enumerate which specific outcomes those are."""

    outcome: str
    reason: str = ""
    key: str | None = None
    trip: dict | None = None
    already_logged: bool = False


@dataclass(frozen=True)
class CandidateResult:
    """Both outcomes for one candidate -- poll_route() ignores this (it
    only needs the stats side effect, matching its existing silent-
    aggregation behavior); scripts/dry_run.py assembles it from
    classify_candidate()'s ClassifyResult plus (for a group winner)
    finish_award_candidate()'s AwardOutcome, for its own verbose
    per-candidate logging and cost/distribution tracking, without needing a
    second copy of the decision logic that produced either half."""

    award: AwardOutcome
    cash: CashOutcome


@dataclass(frozen=True)
class AwardFirstPassResult:
    """The award side of classify_candidate()'s output -- the first-pass
    (weekly-bucket-cash) gate check, computed but NOT yet acted on: no
    dedup, no cap, no Get Trips, no exact-date confirm, no notify. `fired`
    marks a candidate that cleared this gate and is therefore eligible for
    group-winner selection (src/valuation.py's select_group_winners, see
    poll_route()'s and scripts/dry_run.py's loops for how this composes
    with finish_award_candidate() once grouping decides which fired
    candidates actually proceed). `cpp` is the cheap first-pass weekly-
    bucket estimate (0.0 and unused when not fired) -- exactly what
    select_group_winners groups on, so grouping costs nothing new.

    When `fired` is False, `reason` is a complete, already-decided skip
    explanation; the caller finalizes it immediately -- a candidate that
    never even matched the first-pass gate has no claim on group-winner
    selection or the cap in the first place, grouped or not."""

    award: AwardAvailability
    route: RouteConfig
    fired: bool
    cpp: float
    comparable_cash_usd: float | None
    taxes_usd: float | None
    reason: str


@dataclass(frozen=True)
class ClassifyResult:
    """Both outcomes of classify_candidate(): the independent cash trigger
    (fully resolved here -- never deferred or grouped, since it's about the
    FARE observation, not any specific award redemption) and the award's
    first-pass result (may still be pending group-winner selection)."""

    cash: CashOutcome
    award_first_pass: AwardFirstPassResult


def classify_candidate(
    award: AwardAvailability,
    route: RouteConfig,
    config: WatchlistConfig,
    state: StateStore,
    notifier: Notifier,
    cash_update: CashBaselineUpdate | None,
    *,
    max_alerts_per_run: int | None,
    stats: PollStats,
) -> ClassifyResult:
    """Phase 1 of the shared per-candidate decision pipeline (immediate, no
    deferral): the independent cash trigger (mistake-fare ceiling / relative
    drop) and the award's first-pass (weekly-bucket-cash) gate check. Does
    NOT touch dedup/cap/Get-Trips/exact-confirm/notify for the AWARD side --
    that's finish_award_candidate(), called only for the single highest-cpp
    candidate in each (origin, destination, cabin, program, calendar month)
    group, once select_group_winners() has decided who that is (see this
    module's docstring and .claude/skills/deal-valuation's winner-selection
    spec). Both poll_route() (below) and scripts/dry_run.py call this SAME
    function for every candidate, then the SAME select_group_winners() and
    finish_award_candidate() -- neither reimplements any of this.

    `cash_update` is the ALREADY-RESOLVED result of a baseline lookup (or
    None if no provider is wired up, or the lookup failed) -- callers do
    their OWN get_or_refresh_baseline() call, in their OWN try/except,
    before calling this.

    Mutates `stats` in place for everything decided immediately here (the
    cash trigger's outcome, and skipped_other for a candidate that doesn't
    fire the first-pass gate at all) -- group-winner-selection-driven stats
    (skipped_grouped_out, and everything finish_award_candidate() mutates)
    are the CALLER's responsibility, after grouping.

    Does NOT check passes_award_prefilter -- that's deliberately the
    CALLER's responsibility, before the cash-baseline lookup even happens
    (see poll_route()'s and scripts/dry_run.py's own loops), specifically
    so an ineligible-program/wrong-cabin candidate never costs a cash
    lookup in the first place. A version that checked the prefilter here
    instead would still produce the right alerts_sent, but would silently
    waste a real provider call on every ineligible candidate.
    """
    wanted_cabins = set(route.cabins)
    comparable_cash_usd = None
    if cash_update is not None:
        if cash_update.current_fare is not None:
            comparable_cash_usd = cash_update.current_fare.price_usd
        elif cash_update.baseline is not None:
            comparable_cash_usd = cash_update.baseline.ema_usd

    # Cached Search already carries real per-cabin taxes (award.taxes_usd,
    # None when the program doesn't report them) -- no more 0.0 placeholder.
    verdict = is_high_value(
        award, config.awards, wanted_cabins, comparable_cash_usd=comparable_cash_usd, taxes_usd=award.taxes_usd,
    )

    # --- two independent cash triggers, piggybacking on the SAME baseline
    # refresh above (not a separate lookup) -- fire regardless of whether
    # the award itself clears the valuation gate, since a cash signal is
    # meaningful on its own:
    #   1. Absolute mistake-fare ceiling (is_cash_below_mistake_fare_ceiling)
    #      -- fires on ANY fresh observation, including a route's very
    #      FIRST ever one, since it needs no history at all.
    #   2. Relative baseline drop (is_cash_price_drop) -- unchanged, still
    #      requires a pre-existing baseline (previous is not None), so it
    #      never fires on a first observation.
    # Checked in that order and share ONE dedup key (cash_key) since
    # they're both about the same underlying fare observation -- if the
    # ceiling trigger already matched, the drop trigger is skipped rather
    # than double-alerting on the identical price.
    cash_outcome = CashOutcome(outcome="not_triggered")
    if cash_update is not None and cash_update.refreshed and cash_update.current_fare is not None:
        cash_verdict = is_cash_below_mistake_fare_ceiling(cash_update.current_fare.price_usd, config.cash)
        if not cash_verdict.fire and cash_update.previous is not None:
            cash_verdict = is_cash_price_drop(cash_update.current_fare.price_usd, cash_update.previous, config.cash)

        if cash_verdict.fire:
            c_key = cash_key(cash_update.current_fare)
            if state.already_alerted(c_key):
                stats.skipped_duplicate += 1
                cash_outcome = CashOutcome(
                    outcome="duplicate", fare=cash_update.current_fare, verdict=cash_verdict, key=c_key,
                )
            elif max_alerts_per_run is not None and stats.alerts_sent >= max_alerts_per_run:
                stats.skipped_capped += 1
                logger.info(
                    "cash drop %s matched but capped (max_alerts_per_run=%d reached this run), skipping send",
                    c_key, max_alerts_per_run,
                )
                cash_outcome = CashOutcome(
                    outcome="capped", fare=cash_update.current_fare, verdict=cash_verdict, key=c_key,
                    already_logged=True,
                )
            else:
                notifier.send_cash_alert(cash_update.current_fare, cash_verdict, cash_update.previous)
                state.record_alert(c_key, ttl_seconds=config.alerts.dedup_ttl_days * 86400)
                stats.alerts_sent += 1
                stats.cash_alerts_sent += 1
                cash_outcome = CashOutcome(
                    outcome="sent", fare=cash_update.current_fare, verdict=cash_verdict, key=c_key,
                )

    if not verdict.fire:
        stats.skipped_other += 1

    # is_high_value's real implementation guarantees comparable_cash_usd/
    # taxes_usd are non-None whenever fire=True -- this guard is defensive
    # only (e.g. a test double for is_high_value that doesn't preserve that
    # invariant), not a real production path.
    cpp = (
        compute_effective_cpp(comparable_cash_usd, award.taxes_usd, award.miles)
        if verdict.fire and comparable_cash_usd is not None and award.taxes_usd is not None
        else 0.0
    )
    first_pass = AwardFirstPassResult(
        award=award, route=route, fired=verdict.fire, cpp=cpp,
        comparable_cash_usd=comparable_cash_usd, taxes_usd=award.taxes_usd, reason=verdict.reason,
    )
    return ClassifyResult(cash=cash_outcome, award_first_pass=first_pass)


def finish_award_candidate(
    first_pass: AwardFirstPassResult,
    other_dates: list[datetime.date],
    config: WatchlistConfig,
    state: StateStore,
    notifier: Notifier,
    fetch_trip: Callable[[AwardAvailability, float], TripFetchResult],
    *,
    max_alerts_per_run: int | None,
    stats: PollStats,
) -> AwardOutcome:
    """Phase 2 of the shared per-candidate decision pipeline: dedup -> cap
    -> `fetch_trip` (caller-driven Get Trips + exact-date confirm) -> final
    gate -> notify (annotated with `other_dates`, see Notifier.
    send_award_alert's group_other_dates kwarg) + record. Called ONLY for
    the single highest-cpp candidate in its (origin, destination, cabin,
    program, calendar month) group -- see select_group_winners() -- never
    for a candidate that lost that selection (see this module's docstring
    for why `fetch_trip` remains the one caller-supplied extension point).

    `fetch_trip` is called unconditionally once a candidate clears dedup
    and the cap (not just "if comparable_cash_usd is not None" as an
    earlier version had it) -- with the v1.0-style cabin-match-only
    fallback retired, `first_pass.fired` can only be True when
    comparable_cash_usd was already resolved (see src/valuation.py's
    is_high_value), so that conditional was dead code once the fallback
    was removed; simplified away here as a direct, correctness-neutral
    consequence of that fix, not a new behavior change.

    Mutates `stats` in place (matches the existing PollStats contract) --
    both callers already aggregate through a shared, mutable stats object
    across a whole run.
    """
    award = first_pass.award
    route = first_pass.route
    comparable_cash_usd = first_pass.comparable_cash_usd

    key = award_key(award)
    if state.already_alerted(key):
        stats.skipped_duplicate += 1
        return AwardOutcome(outcome="skipped", reason=f"already alerted previously (duplicate), key={key}", key=key)

    # Cap check happens here -- after dedup (so it's independent of dedup,
    # per the design goal: dedup filters repeats across runs, this caps NEW
    # deals within a single run), before Get Trips (so a capped candidate
    # doesn't burn seats.aero quota on a call we already know won't lead to
    # a send). Reported, not dropped silently -- it genuinely matched, it
    # just lost the race for this run's budget.
    if max_alerts_per_run is not None and stats.alerts_sent >= max_alerts_per_run:
        stats.skipped_capped += 1
        logger.info(
            "%s matched but capped (max_alerts_per_run=%d reached this run), skipping send",
            award.availability_id, max_alerts_per_run,
        )
        return AwardOutcome(
            outcome="skipped",
            reason=f"send cap ({max_alerts_per_run}) reached but candidate genuinely matched",
            key=key,
            already_logged=True,
        )

    fetch_result = fetch_trip(award, comparable_cash_usd)
    if fetch_result.trips is None:
        # fetch_trip's caller-specific implementation is responsible for its
        # own logging here (production and dry_run.py want different
        # messages for e.g. an auth failure vs a timeout vs genuinely
        # nothing found) -- always treat as already logged.
        reason = fetch_result.skip_reason or "Get Trips returned nothing (space likely gone)"
        stats.skipped_other += 1
        return AwardOutcome(outcome="skipped", reason=reason, key=key, already_logged=True)

    # Get Trips returns itineraries across ALL cabins on this availability,
    # not just the one we matched -- trips[0] is not guaranteed to be (and
    # in practice often isn't) award.cabin.
    trip = select_trip_for_cabin(fetch_result.trips, award.cabin)
    if trip is None:
        reason = (
            f"no {award.cabin}-cabin trip among {len(fetch_result.trips)} Get Trips result(s), skipping"
        )
        logger.info("%s for %s", reason, award.availability_id)
        stats.skipped_other += 1
        return AwardOutcome(outcome="skipped", reason=reason, key=key, already_logged=True)

    if fetch_result.confirmed_cash_usd is None:
        reason = fetch_result.skip_reason or "no exact-date cash price to confirm the weekly-bucketed estimate"
        if fetch_result.skip_reason is None:
            logger.info("%s: %s, skipping", award.availability_id, reason)
        stats.skipped_other += 1
        return AwardOutcome(outcome="skipped", reason=reason, key=key, already_logged=True)

    # Re-run the gate with Get Trips' taxes AND the exact-date-confirmed
    # cash price above, not the weekly-bucketed one. Cached Search already
    # gave us real taxes earlier (when the program reports them), so this
    # isn't the first place real numbers appear -- it's a confirmation
    # against the more authoritative figures, which can differ (fresher
    # crawl / stale bucket). Skip rather than alert on a stale verdict.
    real_verdict = is_high_value(
        award, config.awards, set(route.cabins),
        comparable_cash_usd=fetch_result.confirmed_cash_usd, taxes_usd=parse_trip_taxes_usd(trip),
    )
    if not real_verdict.fire:
        logger.info("%s no longer clears the gate with real numbers, skipping", award.availability_id)
        stats.skipped_other += 1
        return AwardOutcome(outcome="skipped", reason=real_verdict.reason, key=key, trip=trip, already_logged=True)

    notifier.send_award_alert(
        award, real_verdict, trip,
        transfer_bonus_pct=config.awards.bonus_pct(award.program),
        group_other_dates=other_dates,
    )
    state.record_alert(key, ttl_seconds=config.alerts.dedup_ttl_days * 86400)
    stats.alerts_sent += 1
    return AwardOutcome(outcome="sent", reason=real_verdict.reason, key=key, trip=trip)


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
    start, end = route.date_window.to_dates()
    stats = stats if stats is not None else PollStats()

    def fetch_trip(award: AwardAvailability, comparable_cash_usd: float) -> TripFetchResult:
        # No try/except around Get Trips here -- matches the existing
        # production philosophy of letting seats.aero auth/quota failures
        # propagate loudly rather than being swallowed (see this module's
        # docstring and .claude/skills/aws-serverless-deploy).
        trips = client.get_trips(award.availability_id)
        if not trips:
            logger.info("no trip detail for %s, skipping (space likely gone)", award.availability_id)
            return TripFetchResult(trips=None, confirmed_cash_usd=None)

        try:
            confirmed_cash_usd = confirm_exact_date_price(
                cash_provider, origin=award.origin, destination=award.destination,
                cabin=award.cabin, date=award.date,
            )
        except Exception:
            # A cash-provider hiccup here must not crash the whole poll
            # run -- unlike Get Trips above, this is deliberately a broad
            # catch (auth, rate limit, network), matching the SAME
            # resilience policy the weekly-baseline lookup already has
            # (see .claude/skills/flight-cash-price-monitor).
            logger.warning(
                "exact-date cash confirm failed for %s, skipping (can't verify real CPP)",
                award.availability_id, exc_info=True,
            )
            confirmed_cash_usd = None

        return TripFetchResult(trips=trips, confirmed_cash_usd=confirmed_cash_usd)

    # Phase 1: classify every candidate across this route's origins (cheap
    # cash trigger + first-pass gate, see classify_candidate()) -- NOT yet
    # dedup/cap/Get-Trips/confirm/notify for the award side. Collected here,
    # not acted on per-award, specifically so group-winner selection (below)
    # can see every fired candidate across the WHOLE route before any of
    # them spends a real Get Trips/exact-confirm call.
    classify_results: list[ClassifyResult] = []

    for origin in origins:
        try:
            hits = client.cached_search(origin, route.destinations, start, end, route.cabins)
        except SeatsAeroRateLimitError as exc:
            logger.warning("rate limited on %s from %s, stopping this run: %s", route.name, origin, exc)
            break

        # Observability, not filtering: log every program that actually
        # showed up in real Cached Search results this run, even if the
        # candidate is later rejected by eligible_programs/dedup/cap/CPP --
        # deliberately NOT restricted to config.eligible_programs, so this
        # also surfaces any program seats.aero tracks that isn't in that
        # list at all. After a couple weeks of real runs, use this to see
        # which eligible_programs entries have genuinely produced zero hits
        # before pruning anything -- see watchlist.yaml's eligible_programs
        # comment on why we don't prune based on assumptions today.
        programs_seen = sorted({award.program for award in hits})
        logger.info(
            "%s from %s: %d Cached Search hit(s) across program(s): %s",
            route.name, origin, len(hits), ", ".join(programs_seen) if programs_seen else "none",
        )

        wanted_cabins = set(route.cabins)
        for award in hits:
            stats.candidates_evaluated += 1

            # Cheap, pure-function check before spending a (cheap but not
            # free) baseline lookup on an award we'd reject anyway -- Cached
            # Search is already scoped to route.cabins, so this rarely
            # actually filters anything out in practice, but it's a real
            # skip when it does. Deliberately NOT inside classify_candidate
            # -- see that function's docstring for why.
            if not passes_award_prefilter(
                award, wanted_cabins, config.eligible_programs, config.awards.premium_cabin_max_multiplier,
            ):
                stats.skipped_other += 1
                continue

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
                    # not crash the whole poll run -- but it also must NOT
                    # fall back to firing on cabin-match alone (see
                    # src/valuation.py's is_high_value module docstring for
                    # why that fallback direction was retired). The award
                    # simply skips this run, same as any other unresolved
                    # cash lookup. See .claude/skills/flight-cash-price-monitor.
                    logger.warning(
                        "cash baseline lookup failed for %s->%s (%s) on %s -- cash data unavailable, "
                        "skipping rather than firing blind",
                        award.origin, award.destination, award.cabin, award.date, exc_info=True,
                    )

            classify_results.append(
                classify_candidate(
                    award, route, config, state, notifier, cash_update,
                    max_alerts_per_run=max_alerts_per_run, stats=stats,
                )
            )

    # Phase 2: group-winner selection -- prevents near-duplicate dates of
    # one deal (same origin/destination/cabin/program/calendar month) from
    # each independently spending a Get Trips + exact-confirm call and
    # competing for the per-run alert cap. Scoped to this route's own
    # candidates (across all its origins), not the whole multi-route run()
    # invocation -- the real watchlist.yaml's active routes have disjoint
    # destination sets, so this is behaviorally equivalent to a whole-run
    # scope for the current config; see .claude/skills/deal-valuation's
    # winner-selection spec for why per-route was chosen over restructuring
    # run()'s cross-route contract for a difference that doesn't exist
    # today. See select_group_winners() for the grouping/tie-break mechanism
    # (identical to what src/digest.py applies before its own ranking).
    fired = [r.award_first_pass for r in classify_results if r.award_first_pass.fired]
    group_winners = select_group_winners(fired)
    winner_ids = {id(first_pass) for first_pass, _ in group_winners}
    for r in classify_results:
        if r.award_first_pass.fired and id(r.award_first_pass) not in winner_ids:
            stats.skipped_grouped_out += 1
            logger.info(
                "%s grouped out -- lost to a higher-cpp same-route/cabin/program/month candidate this run",
                r.award_first_pass.award.availability_id,
            )

    for first_pass, other_dates in group_winners:
        finish_award_candidate(
            first_pass, other_dates, config, state, notifier, fetch_trip,
            max_alerts_per_run=max_alerts_per_run, stats=stats,
        )

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
            # Per-route origins override falls back to the top-level list
            # when absent -- see RouteConfig.origins and watchlist.yaml.
            route_origins = route.origins if route.origins is not None else config.origins
            poll_route(
                seats_client,
                route_origins,
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
        "%d skipped (cap reached), %d grouped out (lost to a same-route/cabin/program/month candidate)",
        stats.candidates_evaluated, stats.alerts_sent, stats.cash_alerts_sent,
        stats.skipped_duplicate, stats.skipped_capped, stats.skipped_grouped_out,
    )
    return stats.alerts_sent


def run_digest(
    config: WatchlistConfig | None = None,
    *,
    seats_client: SeatsAeroClient | None = None,
    cash_provider: CashFareProvider | None = None,
    state: StateStore | None = None,
    notifier: Notifier | None = None,
) -> DigestResult:
    """Lambda-facing wrapper for the weekly digest -- mirrors run()'s own
    real-client-construction/close pattern (same secrets, same real
    DynamoDB tables via the same env vars, same notifier-selection logic),
    but delegates the actual per-candidate ranking to src/digest.py's
    build_weekly_digest() rather than poll_route()'s classify_candidate()/
    finish_award_candidate() pair. See src/digest.py's module docstring for
    why the digest needs its own orchestration instead of reusing those
    (different shape: ranks everything and sends one aggregate message, no
    dedup, no per-run cap).

    Deliberately does NOT emit the CloudWatch heartbeat run() emits: the
    heartbeat exists to distinguish "no alerts" from "the poller is dead" on
    the real-time path (see this module's docstring) -- the digest always
    sends something every week by design, so a missing digest message is
    already its own distinct failure signal. Wiring the same heartbeat
    metric here would blur, not sharpen, that distinction.
    """
    config = config or load_watchlist()
    owns_seats_client = seats_client is None
    owns_cash_provider = cash_provider is None
    owns_notifier = notifier is None
    seats_client = seats_client or SeatsAeroClient(secrets.get_seats_aero_api_key())
    cash_provider = cash_provider or SerpApiClient(secrets.get_serpapi_key())
    # Same tables run() uses -- the digest only ever reads/updates the cash
    # baseline cache (via build_weekly_digest's get_or_refresh_baseline
    # calls), never the alerts/dedup table, but StateStore's DynamoDB impl
    # is constructed with both, same as production. No new infra required.
    state = state or DynamoStateStore(
        alerts_table=_require_env("ALERTS_TABLE_NAME", "DynamoDB alerts/dedup table name"),
        baselines_table=_require_env("BASELINES_TABLE_NAME", "DynamoDB cash-baselines table name"),
    )
    notifier = notifier or _build_notifier(config.notifier)

    try:
        result = build_weekly_digest(config, seats_client, cash_provider, state)
        notifier.send_digest(result)
    finally:
        if owns_seats_client:
            seats_client.close()
        if owns_cash_provider:
            cash_provider.close()
        if owns_notifier:
            notifier.close()

    logger.info(
        "digest complete: %d candidate(s) evaluated, %d ranked, %d cash-rank entr(y/ies), %d cpp-rank entr(y/ies)",
        result.candidates_evaluated, result.candidates_ranked, len(result.cash_rank), len(result.cpp_rank),
    )
    return result


def lambda_handler(event, context):
    # Distinct event payload dispatches to the weekly digest instead of the
    # normal real-time cached-poll path -- a second EventBridge schedule on
    # this SAME Lambda invokes with {"mode": "digest"}, no new Lambda, no new
    # deployment artifact (see deal-valuation's digest spec). Any other
    # event shape (including the existing schedule's default payload, or a
    # manual invoke with no event at all) preserves the exact pre-digest
    # behavior below -- zero change to the real-time path.
    if isinstance(event, dict) and event.get("mode") == "digest":
        result = run_digest()
        return {
            "statusCode": 200,
            "mode": "digest",
            "candidatesEvaluated": result.candidates_evaluated,
            "candidatesRanked": result.candidates_ranked,
        }
    alerts_sent = run()
    return {"statusCode": 200, "alertsSent": alerts_sent}
