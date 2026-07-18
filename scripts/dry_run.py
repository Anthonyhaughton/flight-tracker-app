#!/usr/bin/env python3
"""End-to-end dry run: real seats.aero Cached Search -> cash baseline
lookup/refresh -> valuation gate (real CPP once cash is available) ->
Get Trips -> exact-date cash confirm -> a real Discord embed in your actual
channel. Also exercises the independent cash-drop and mistake-fare-ceiling
triggers.

Exercises the SAME real components src/poller.py's poll_route() uses
(SeatsAeroClient, SerpApiClient, get_or_refresh_baseline,
confirm_exact_date_price, is_high_value, is_cash_price_drop,
is_cash_below_mistake_fare_ceiling, award_key, cash_key, DiscordNotifier),
rather than calling poll_route() directly, so this script can enforce its
own explicit per-failure-mode messages -- which poll_route() doesn't have,
by design (production DOES want auth/quota errors to propagate loudly
rather than being swallowed with a friendly message).

Targets ONE real configured route from watchlist.yaml, selected via
--route "<route name>" (required -- no default, deliberately, so a bare
invocation can never accidentally trigger whichever route happens to be
first/largest). Reads that route's real origins (falling back to the
top-level list, same as production), destinations, cabins, date_window, and
require_cash_comparison directly off the loaded config -- NOT separate
hardcoded constants. An earlier version of this script hardcoded its own
ORIGIN/DESTINATION/CABINS/WINDOW constants instead of reading them from
watchlist.yaml, which is exactly the kind of drift that has bitten this
script twice before (missed a trips[0] regression, and later never gained
the v1.1 cash wiring at all, so it silently kept exercising v1.0-only
behavior even after production had real CPP gating). Reading the real route
config directly removes that entire class of drift.

Real API call budget varies substantially by --route, since routes have very
different fan-out (e.g. DC -> Italy: 1 origin x 1 destination; DC -> Europe
(broad): 2 origins x 8 destinations = 16 origin-destination pairs). Printed
as a pre-flight estimate before any real call is made:
  - seats.aero Cached Search: EXACTLY len(origins) x len(destinations) calls
    (cached_search() issues one HTTP GET per destination internally, once
    per origin) -- deterministic, not an estimate.
  - seats.aero Get Trips: 0 to --max-alerts calls (one per candidate that
    clears the first-pass gate, isn't a dedup duplicate, and isn't
    cap-blocked).
  - SerpApi weekly-baseline lookups: one real call per distinct
    (route, cabin, ISO-week) bucket among prefilter-passing candidates that
    isn't already cached -- NOT bounded by --max-alerts (matches
    production: the cap only blocks Get Trips/sends, not the cheap
    first-pass baseline lookup) -- genuinely unpredictable before running
    for a wide route, since it depends on how much real award space exists
    across the whole window.
  - SerpApi exact-date confirms: 0 to --max-alerts calls (one per candidate
    that reaches Get Trips).
  - The mistake-fare-ceiling trigger makes NO separate SerpApi call of its
    own -- it's evaluated against the SAME weekly-baseline call's result as
    the relative-drop trigger, so it costs nothing incremental.

Unlike an earlier version, the send cap (--max-alerts, defaults to the REAL
config.alerts.max_alerts_per_run) no longer stops the script from evaluating
further candidates once hit -- it now CONTINUES through the cheap
first-pass gate (mirroring poll_route()'s real continue-not-break behavior)
so skipped_capped accurately counts every candidate that genuinely matched
but lost the race for this run's send budget, without spending a Get
Trips/exact-confirm call on any of them. The previous break-on-cap behavior
made it structurally impossible to answer "is the cap actually limiting
real coverage" from a single run.

SerpApi network timeouts (httpx.TimeoutException, e.g. slow upstream data on
a far-future date) are caught as SerpApiTimeoutError and treated as a
transient, non-fatal skip of that one candidate (skipped_timeout, logged and
counted distinctly from a genuine value-gate rejection) -- NOT propagated as
a crash, unlike auth/quota failures which genuinely mean stop. An earlier
version let a raw httpx.ReadTimeout crash the whole script uncaught.

--cpp-floor and --min-trip-value override config.awards for THIS run only
(in-memory dataclasses.replace, never touches watchlist.yaml -- nothing to
revert). Pass either to test a proposed threshold against real data before
committing it to config. Overriding either does not change the SerpApi call
estimate -- the baseline lookup happens before the value gate is evaluated.

The end-of-run summary also reports the real min/median/max CPP and trip-value
across every candidate where both were actually computed (pass or fail), so
"how close are the current thresholds" is answerable from one run's real
data rather than guessing or re-running with different values.

Not wired into the poller or scheduler. Run manually:

    python scripts/dry_run.py --route "DC → Italy"
    python scripts/dry_run.py --route "DC → Europe (broad)"
    python scripts/dry_run.py --route "DC → Europe (broad)" --max-alerts 3
    python scripts/dry_run.py --route "DC → Europe (broad)" --origins IAD --destinations LHR --cpp-floor 2.0 --min-trip-value 250

Dedup + baseline state persists to scripts/.dry_run_state.json (gitignored)
across runs of this script specifically -- production uses DynamoDB
(src/state.py); this is a local stand-in so a second run of this script
doesn't re-alert the same deal, without needing an AWS account set up yet.
Shared across ALL routes run through this script (keyed by real
origin/destination/cabin/date, same as production), not per-route.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import logging
import os
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src import secrets  # noqa: E402
from src.cash import confirm_exact_date_price, get_or_refresh_baseline  # noqa: E402
from src.config import load_watchlist  # noqa: E402
from src.notify.discord import DiscordError, DiscordNotifier  # noqa: E402
from src.providers.cash.serpapi import (  # noqa: E402
    SerpApiAuthError,
    SerpApiClient,
    SerpApiRateLimitError,
    SerpApiTimeoutError,
)
from src.providers.seats_aero import (  # noqa: E402
    SeatsAeroAuthError,
    SeatsAeroClient,
    SeatsAeroRateLimitError,
    parse_trip_taxes_usd,
    select_trip_for_cabin,
)
from src.state import Baseline, award_key, cash_key, compute_updated_baseline  # noqa: E402
from src.valuation import (  # noqa: E402
    compute_effective_cpp,
    is_cash_below_mistake_fare_ceiling,
    is_cash_price_drop,
    is_high_value,
    passes_award_prefilter,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("dry_run")

# httpx logs "HTTP Request: <method> <full URL> ..." at INFO by default.
# That's harmless for seats.aero (key is a header, not part of the URL) but
# NOT for Discord webhooks or SerpApi -- the webhook URL and the SerpApi
# api_key are both part of the URL/query string, so logging it would leak a
# real credential to stdout/logs. Silence httpx's own request logger; our
# explicit logger.info() calls below are unaffected.
logging.getLogger("httpx").setLevel(logging.WARNING)

STATE_FILE_PATH = Path(__file__).resolve().parent / ".dry_run_state.json"


class FileStateStore:
    """Local, JSON-file-backed StateStore for this script only. Production
    uses DynamoDB (src/state.py) -- this exists purely so a second run of
    this script doesn't re-alert a deal (or re-treat a stale baseline as
    fresh) it already saw, without requiring an AWS account to be set up
    yet.

    update_baseline delegates the actual trailing-min/EMA math to
    src.state.compute_updated_baseline -- the SAME function DynamoStateStore
    and InMemoryStateStore use -- so this store can't silently drift from
    production's real baseline semantics."""

    def __init__(self, path: Path):
        self._path = path
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            return json.loads(self._path.read_text())
        return {"alerts": {}, "baselines": {}}

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._data, indent=2))

    def already_alerted(self, key: str) -> bool:
        expires_at = self._data["alerts"].get(key)
        if expires_at is None:
            return False
        if expires_at < time.time():
            del self._data["alerts"][key]
            self._save()
            return False
        return True

    def record_alert(self, key: str, ttl_seconds: int) -> None:
        self._data["alerts"][key] = time.time() + ttl_seconds
        self._save()

    def get_baseline(self, route_key: str) -> Baseline | None:
        item = self._data["baselines"].get(route_key)
        if not item:
            return None
        return Baseline(
            trailing_min_usd=item["trailing_min_usd"],
            ema_usd=item["ema_usd"],
            updated_at=datetime.datetime.fromisoformat(item["updated_at"]),
        )

    def update_baseline(self, route_key: str, price: float) -> None:
        existing = self.get_baseline(route_key)
        new_baseline = compute_updated_baseline(existing, price, datetime.datetime.now(datetime.timezone.utc))
        self._data["baselines"][route_key] = {
            "trailing_min_usd": new_baseline.trailing_min_usd,
            "ema_usd": new_baseline.ema_usd,
            "updated_at": new_baseline.updated_at.isoformat(),
        }
        self._save()


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader. Never logs or returns values -- only sets
    os.environ, and only for keys not already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def main() -> int:
    _load_dotenv(REPO_ROOT / ".env")

    try:
        seats_api_key = secrets.get_seats_aero_api_key()
        serpapi_key = secrets.get_serpapi_key()
        discord_webhook_url = secrets.get_discord_webhook_url()
    except secrets.MissingSecretError as exc:
        logger.error(str(exc))
        return 1

    config = load_watchlist()  # real min_trip_value_usd / cpp_floors / eligible_programs / dedup_ttl_days / etc.

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--route", required=True, choices=[r.name for r in config.routes],
        help="Real route name from watchlist.yaml to exercise -- required, no default.",
    )
    parser.add_argument(
        "--max-alerts", type=int, default=None,
        help="Send cap for this run. Defaults to the REAL config.alerts.max_alerts_per_run "
        "(so this script can validate whether that cap is sufficient for a route this size) "
        "-- pass a smaller number to be more conservative on a first-ever test of a new route.",
    )
    parser.add_argument(
        "--origins", type=str, default=None,
        help="Comma-separated origin override for THIS run only (e.g. 'IAD') -- scopes down a "
        "wide route's fan-out for a cheaper first look, without editing watchlist.yaml. Defaults "
        "to the route's real origins (its own override, or the top-level list).",
    )
    parser.add_argument(
        "--destinations", type=str, default=None,
        help="Comma-separated destination override for THIS run only (e.g. 'LHR,BCN') -- scopes "
        "down a wide route's fan-out for a cheaper first look, without editing watchlist.yaml. "
        "Defaults to the route's real destinations.",
    )
    parser.add_argument(
        "--cpp-floor", type=float, default=None,
        help="Override EVERY program's cpp_floor (including 'default') to this single value, for "
        "THIS run only -- does not touch watchlist.yaml, no revert needed. For testing a proposed "
        "threshold change against real data before committing it to config.",
    )
    parser.add_argument(
        "--min-trip-value", type=float, default=None,
        help="Override min_trip_value_usd for THIS run only -- does not touch watchlist.yaml, no "
        "revert needed.",
    )
    args = parser.parse_args()

    route = next(r for r in config.routes if r.name == args.route)
    if args.origins is not None:
        origins = [o.strip() for o in args.origins.split(",") if o.strip()]
    else:
        origins = route.origins if route.origins is not None else config.origins
    if args.destinations is not None:
        destinations = [d.strip() for d in args.destinations.split(",") if d.strip()]
    else:
        destinations = route.destinations
    cabins = route.cabins
    wanted_cabins = set(cabins)
    start, end = route.date_window.to_dates()
    max_alerts = args.max_alerts if args.max_alerts is not None else config.alerts.max_alerts_per_run

    # --cpp-floor / --min-trip-value are in-memory overrides ONLY -- config.awards
    # itself (and watchlist.yaml) is never mutated, so there is nothing to
    # revert regardless of how this run ends.
    awards_config = config.awards
    if args.cpp_floor is not None:
        awards_config = dataclasses.replace(
            awards_config, cpp_floors={k: args.cpp_floor for k in awards_config.cpp_floors}
        )
    if args.min_trip_value is not None:
        awards_config = dataclasses.replace(awards_config, min_trip_value_usd=args.min_trip_value)

    est_cached_search_calls = len(origins) * len(destinations)
    logger.info(
        "Route: %r | origins=%s destinations=%s cabins=%s dates=%s..%s | require_cash_comparison=%s | "
        "eligible_programs=%s | send cap=%d",
        route.name, origins, destinations, cabins, start.isoformat(), end.isoformat(),
        route.require_cash_comparison, config.eligible_programs, max_alerts,
    )
    if args.cpp_floor is not None or args.min_trip_value is not None:
        logger.info(
            "THRESHOLD OVERRIDE active for this run only (watchlist.yaml untouched): cpp_floor=%s "
            "(real config: %s), min_trip_value_usd=%s (real config: $%s)",
            args.cpp_floor if args.cpp_floor is not None else "unchanged", config.awards.cpp_floors,
            args.min_trip_value if args.min_trip_value is not None else "unchanged", config.awards.min_trip_value_usd,
        )
    logger.info(
        "PRE-FLIGHT: this run will issue EXACTLY %d seats.aero Cached Search call(s) "
        "(%d origin(s) x %d destination(s)), plus up to %d Get Trips call(s) (bounded by the send "
        "cap). SerpApi weekly-baseline call count is NOT bounded by the send cap (matches production) "
        "and cannot be predicted exactly before running -- it depends on how many distinct "
        "(route, cabin, ISO-week) buckets have real eligible award space across the whole window. "
        "Note: threshold overrides (--cpp-floor/--min-trip-value) do NOT change this estimate -- the "
        "baseline lookup happens before the value gate is evaluated, so a looser threshold doesn't "
        "cost more SerpApi calls, only changes how many candidates pass afterward.",
        est_cached_search_calls, len(origins), len(destinations), max_alerts,
    )

    client = SeatsAeroClient(seats_api_key, max_retries=0)
    cash_client = SerpApiClient(serpapi_key, max_retries=0)
    notifier = DiscordNotifier(discord_webhook_url)
    state = FileStateStore(STATE_FILE_PATH)

    candidates_seen = 0
    sent = 0
    cash_drop_sent = 0
    ceiling_sent = 0
    skipped_duplicate = 0
    skipped_capped = 0
    skipped_other = 0
    skipped_timeout = 0
    cached_search_calls = 0
    get_trips_calls = 0
    serpapi_weekly_calls = 0
    serpapi_confirm_calls = 0
    programs_seen_overall: set[str] = set()
    # Rejected-value distribution: every real (comparable_cash_usd, taxes_usd)
    # pair actually computed, regardless of pass/fail, from BOTH the
    # first-pass gate and the post-Get-Trips recheck -- lets a threshold test
    # report min/median/max, not just pass/fail counts, so "how close are we"
    # is answerable from real data instead of re-running with different values.
    computed_cpps: list[float] = []
    computed_trip_values: list[float] = []

    try:
        for origin in origins:
            try:
                hits = client.cached_search(origin, destinations, start, end, cabins)
            except SeatsAeroAuthError:
                logger.error(
                    "AUTH FAILED (401/403): seats.aero rejected the key, or Partner API access isn't "
                    "enabled on this account. Check the 'API' tab under https://seats.aero account settings."
                )
                return 1
            except SeatsAeroRateLimitError:
                logger.error(
                    "RATE LIMITED (429): today's 1,000-call daily quota is exhausted. It resets at "
                    "00:00 UTC -- nothing to do but wait."
                )
                return 1

            cached_search_calls += len(destinations)  # cached_search() issues one HTTP GET per destination
            logger.info("X-RateLimit-Remaining: %s", client.last_rate_limit_remaining)
            programs_this_origin = sorted({h.program for h in hits})
            programs_seen_overall.update(programs_this_origin)
            logger.info(
                "%s from %s: %d hit(s) across program(s): %s",
                route.name, origin, len(hits), ", ".join(programs_this_origin) or "none",
            )

            for award in hits:
                candidates_seen += 1

                if not passes_award_prefilter(award, wanted_cabins, config.eligible_programs):
                    skipped_other += 1
                    continue

                try:
                    cash_update = get_or_refresh_baseline(
                        state, cash_client, origin=award.origin, destination=award.destination, cabin=award.cabin,
                        date=award.date, max_age_minutes=config.schedule.cash_baseline_minutes,
                    )
                except SerpApiAuthError:
                    logger.error("AUTH FAILED (401): SerpApi rejected the key. Check https://serpapi.com/manage-api-key.")
                    return 1
                except SerpApiRateLimitError:
                    logger.error(
                        "RATE LIMITED / QUOTA EXHAUSTED (429) on SerpApi -- check remaining searches at "
                        "https://serpapi.com/account."
                    )
                    return 1
                except SerpApiTimeoutError as exc:
                    logger.warning(
                        "TIMEOUT on SerpApi weekly-baseline lookup for %s->%s (%s) on %s -- transient, "
                        "skipping this candidate (not fatal, unlike auth/quota failures): %s",
                        award.origin, award.destination, award.cabin, award.date, exc,
                    )
                    skipped_timeout += 1
                    continue

                if cash_update.refreshed:
                    serpapi_weekly_calls += 1

                comparable_cash_usd = None
                if cash_update.current_fare is not None:
                    comparable_cash_usd = cash_update.current_fare.price_usd
                    logger.info(
                        "cash baseline REFRESHED for %s (weekly bucket): $%.0f (real SerpApi call)",
                        cash_update.key, cash_update.current_fare.price_usd,
                    )
                elif cash_update.baseline is not None:
                    comparable_cash_usd = cash_update.baseline.ema_usd
                    logger.info("cash baseline CACHED for %s: $%.0f typical (no SerpApi call)", cash_update.key, comparable_cash_usd)
                else:
                    logger.info("no cash data available for %s", cash_update.key)

                if comparable_cash_usd is not None and award.taxes_usd is not None:
                    computed_trip_values.append(comparable_cash_usd - award.taxes_usd)
                    computed_cpps.append(compute_effective_cpp(comparable_cash_usd, award.taxes_usd, award.miles))

                verdict = is_high_value(
                    award, awards_config, wanted_cabins, comparable_cash_usd=comparable_cash_usd, taxes_usd=award.taxes_usd,
                    require_cash_comparison=route.require_cash_comparison,
                )

                # --- two independent cash triggers, piggybacking on the SAME
                # baseline refresh above -- see src/poller.py's poll_route()
                # for the identical shape. Ceiling checked first (fires even
                # on a route's first-ever observation); drop trigger only if
                # ceiling didn't fire AND previous is not None (seeded
                # silently otherwise). Both share one dedup key. NEITHER is
                # gated by the send cap being reached in this section --
                # matches poll_route()'s real ordering (cap only blocks the
                # actual send, never the cheap evaluation).
                if cash_update.refreshed and cash_update.current_fare is not None:
                    cash_verdict = is_cash_below_mistake_fare_ceiling(cash_update.current_fare.price_usd, config.cash)
                    is_ceiling = cash_verdict.fire
                    if not cash_verdict.fire and cash_update.previous is not None:
                        cash_verdict = is_cash_price_drop(cash_update.current_fare.price_usd, cash_update.previous, config.cash)

                    if cash_verdict.fire:
                        c_key = cash_key(cash_update.current_fare)
                        if state.already_alerted(c_key):
                            logger.info("SKIP cash %s: already alerted previously (duplicate)", c_key)
                            skipped_duplicate += 1
                        elif sent >= max_alerts:
                            logger.info("SKIP cash %s: send cap (%d) reached but candidate genuinely matched", c_key, max_alerts)
                            skipped_capped += 1
                        else:
                            try:
                                notifier.send_cash_alert(cash_update.current_fare, cash_verdict, cash_update.previous)
                            except DiscordError as exc:
                                logger.error("DISCORD SEND FAILED for cash %s: %s", c_key, exc)
                                return 1
                            state.record_alert(c_key, ttl_seconds=config.alerts.dedup_ttl_days * 86400)
                            sent += 1
                            if is_ceiling:
                                ceiling_sent += 1
                            else:
                                cash_drop_sent += 1
                            logger.info(
                                "SENT cash %s: $%.0f (%s)", c_key, cash_update.current_fare.price_usd, cash_verdict.reason,
                            )

                if not verdict.fire:
                    logger.info("SKIP %s: %s", award.availability_id, verdict.reason)
                    skipped_other += 1
                    continue

                key = award_key(award)
                if state.already_alerted(key):
                    logger.info("SKIP %s: already alerted previously (duplicate), key=%s", award.availability_id, key)
                    skipped_duplicate += 1
                    continue

                if sent >= max_alerts:
                    logger.info(
                        "SKIP %s: send cap (%d) reached but candidate genuinely cleared the first-pass gate "
                        "(no Get Trips call spent)", award.availability_id, max_alerts,
                    )
                    skipped_capped += 1
                    continue

                try:
                    trips = client.get_trips(award.availability_id)
                except SeatsAeroAuthError:
                    logger.error("AUTH FAILED (401/403) on Get Trips for %s", award.availability_id)
                    return 1
                except SeatsAeroRateLimitError:
                    logger.error(
                        "RATE LIMITED (429) on Get Trips for %s -- quota was exhausted mid-run", award.availability_id
                    )
                    return 1

                get_trips_calls += 1
                logger.info("X-RateLimit-Remaining: %s", client.last_rate_limit_remaining)

                if not trips:
                    logger.info("SKIP %s: Get Trips returned nothing (space likely gone)", award.availability_id)
                    skipped_other += 1
                    continue

                # Get Trips returns itineraries across ALL cabins on this
                # availability (confirmed live: 88 trips for one business-cabin
                # hit) -- trips[0] is not guaranteed to be award.cabin.
                trip = select_trip_for_cabin(trips, award.cabin)
                if trip is None:
                    logger.info(
                        "SKIP %s: no %s-cabin trip among %d Get Trips result(s)",
                        award.availability_id, award.cabin, len(trips),
                    )
                    skipped_other += 1
                    continue

                # Exact-date cash confirm: the weekly-bucketed baseline above is
                # accurate enough to decide candidacy, but not to gate a real
                # send -- day-of-week price variance can be large. Spend ONE
                # additional real SerpApi call for this award's EXACT date, only
                # now that the candidate has cleared everything else. See
                # src/cash.py's confirm_exact_date_price and src/poller.py's
                # matching step.
                if comparable_cash_usd is not None:
                    try:
                        confirmed_cash_usd = confirm_exact_date_price(
                            cash_client, origin=award.origin, destination=award.destination,
                            cabin=award.cabin, date=award.date,
                        )
                    except SerpApiAuthError:
                        logger.error("AUTH FAILED (401) on SerpApi exact-date confirm for %s", award.availability_id)
                        return 1
                    except SerpApiRateLimitError:
                        logger.error(
                            "RATE LIMITED / QUOTA EXHAUSTED (429) on SerpApi exact-date confirm for %s",
                            award.availability_id,
                        )
                        return 1
                    except SerpApiTimeoutError as exc:
                        logger.warning(
                            "TIMEOUT on SerpApi exact-date confirm for %s -- transient, skipping this "
                            "candidate (not fatal, unlike auth/quota failures): %s", award.availability_id, exc,
                        )
                        skipped_timeout += 1
                        continue

                    serpapi_confirm_calls += 1

                    if confirmed_cash_usd is None:
                        logger.info(
                            "SKIP %s: no exact-date cash price to confirm the weekly-bucketed estimate",
                            award.availability_id,
                        )
                        skipped_other += 1
                        continue

                    logger.info(
                        "cash EXACT-DATE CONFIRM for %s: $%.0f (weekly estimate was $%.0f) (real SerpApi call)",
                        award.availability_id, confirmed_cash_usd, comparable_cash_usd,
                    )
                    comparable_cash_usd = confirmed_cash_usd

                real_taxes_usd = parse_trip_taxes_usd(trip)
                if comparable_cash_usd is not None:
                    computed_trip_values.append(comparable_cash_usd - real_taxes_usd)
                    computed_cpps.append(compute_effective_cpp(comparable_cash_usd, real_taxes_usd, award.miles))

                real_verdict = is_high_value(
                    award, awards_config, wanted_cabins,
                    comparable_cash_usd=comparable_cash_usd, taxes_usd=real_taxes_usd,
                    require_cash_comparison=route.require_cash_comparison,
                )
                if not real_verdict.fire:
                    logger.info("SKIP %s: failed real-numbers recheck (%s)", award.availability_id, real_verdict.reason)
                    skipped_other += 1
                    continue

                try:
                    notifier.send_award_alert(award, real_verdict, trip)
                except DiscordError as exc:
                    logger.error("DISCORD SEND FAILED for %s: %s", award.availability_id, exc)
                    return 1

                state.record_alert(key, ttl_seconds=config.alerts.dedup_ttl_days * 86400)
                sent += 1
                logger.info(
                    "SENT %s: %s %s->%s, %s miles",
                    award.availability_id, award.cabin, award.origin, award.destination, f"{trip['MileageCost']:,}",
                )

        logger.info(
            "Done. route=%r %d candidate(s) seen across program(s) %s. %d sent (%d award, %d cash-drop, "
            "%d mistake-fare-ceiling), %d skipped-as-duplicate, %d skipped-capped (genuinely matched, "
            "lost the send-cap race), %d skipped-timeout (transient, not a real rejection), %d skipped-other.",
            route.name, candidates_seen, sorted(programs_seen_overall),
            sent, sent - cash_drop_sent - ceiling_sent, cash_drop_sent, ceiling_sent,
            skipped_duplicate, skipped_capped, skipped_timeout, skipped_other,
        )
        logger.info(
            "Real call totals -- seats.aero: %d Cached Search + %d Get Trips = %d. "
            "SerpApi: %d weekly-baseline + %d exact-date-confirm = %d.",
            cached_search_calls, get_trips_calls, cached_search_calls + get_trips_calls,
            serpapi_weekly_calls, serpapi_confirm_calls, serpapi_weekly_calls + serpapi_confirm_calls,
        )
        logger.info("seats.aero X-RateLimit-Remaining (final): %s", client.last_rate_limit_remaining)
        if computed_cpps:
            logger.info(
                "Value distribution across %d candidate(s) with real cash data (regardless of pass/fail) -- "
                "CPP: min=%.2fcpp median=%.2fcpp max=%.2fcpp | Trip value: min=$%.0f median=$%.0f max=$%.0f",
                len(computed_cpps),
                min(computed_cpps), statistics.median(computed_cpps), max(computed_cpps),
                min(computed_trip_values), statistics.median(computed_trip_values), max(computed_trip_values),
            )
        else:
            logger.info("No candidate had both a resolved cash price and known taxes -- no value distribution to report.")
        if skipped_capped > 0:
            logger.info(
                "%d candidate(s) genuinely cleared the first-pass gate but lost the send-cap (%d) race this run -- "
                "the cap IS limiting real coverage for this route at this cap value.",
                skipped_capped, max_alerts,
            )
        if sent > 0:
            logger.info("Check Discord -- %d embed(s) should have landed in the channel.", sent)
        else:
            logger.info("Nothing sent this run.")
        return 0
    finally:
        client.close()
        cash_client.close()
        notifier.close()


if __name__ == "__main__":
    sys.exit(main())
