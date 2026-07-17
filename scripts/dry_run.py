#!/usr/bin/env python3
"""End-to-end dry run: real seats.aero Cached Search -> cash baseline
lookup/refresh -> valuation gate (real CPP once cash is available) ->
Get Trips -> exact-date cash confirm -> a real Discord embed in your actual
channel. Also exercises the independent cash-drop trigger.

Exercises the SAME real components src/poller.py's poll_route() uses
(SeatsAeroClient, SerpApiClient, get_or_refresh_baseline,
confirm_exact_date_price, is_high_value, is_cash_price_drop, award_key,
cash_key, DiscordNotifier) for exactly one route (IAD -> FCO, business),
rather than calling poll_route() directly, so this script can enforce its
own hard spam cap and give explicit per-failure-mode messages -- neither of
which poll_route() has, by design (production doesn't want a silent alert
cap, and DOES want auth/quota errors to propagate loudly rather than being
swallowed with a friendly message).

This mirrors production's real flow deliberately -- an earlier version of
this script drifted out of sync with poll_route() (missed a trips[0]
regression, and later never gained the v1.1 cash wiring at all, so it was
silently still exercising v1.0 cabin-match-only behavior even after
production had real CPP gating). Keep this in sync with poll_route()'s
shape when that changes.

Real API call budget:
  - seats.aero: exactly 1 Cached Search call, plus up to MAX_ALERTS Get
    Trips calls (one per candidate that clears the first-pass gate, isn't a
    dedup duplicate, and isn't cap-blocked) -- so at most 1 + MAX_ALERTS
    seats.aero calls.
  - SerpApi: one weekly-bucketed baseline lookup per distinct route+cabin+
    ISO-week among prefilter-passing candidates (almost always 1, since
    this script's ~1-month window spans only a few ISO weeks and every
    candidate shares the same route+cabin) -- cached across candidates in
    the same bucket, exactly like production -- PLUS up to MAX_ALERTS
    exact-date confirm calls (one per candidate that reaches Get Trips).
    So at most a handful of weekly lookups + MAX_ALERTS confirms.

Not wired into the poller or scheduler. Run manually:

    python scripts/dry_run.py

Dedup + baseline state persists to scripts/.dry_run_state.json (gitignored)
across runs of this script specifically -- production uses DynamoDB
(src/state.py); this is a local stand-in so a second run of this script
doesn't re-alert the same deal, without needing an AWS account set up yet.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src import secrets  # noqa: E402
from src.cash import confirm_exact_date_price, get_or_refresh_baseline  # noqa: E402
from src.config import load_watchlist  # noqa: E402
from src.notify.discord import DiscordError, DiscordNotifier  # noqa: E402
from src.providers.cash.serpapi import SerpApiAuthError, SerpApiClient, SerpApiRateLimitError  # noqa: E402
from src.providers.seats_aero import (  # noqa: E402
    SeatsAeroAuthError,
    SeatsAeroClient,
    SeatsAeroRateLimitError,
    parse_trip_taxes_usd,
    select_trip_for_cabin,
)
from src.state import Baseline, award_key, cash_key, compute_updated_baseline  # noqa: E402
from src.valuation import is_cash_price_drop, is_high_value, passes_award_prefilter  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("dry_run")

# httpx logs "HTTP Request: <method> <full URL> ..." at INFO by default.
# That's harmless for seats.aero (key is a header, not part of the URL) but
# NOT for Discord webhooks or SerpApi -- the webhook URL and the SerpApi
# api_key are both part of the URL/query string, so logging it would leak a
# real credential to stdout/logs. Silence httpx's own request logger; our
# explicit logger.info() calls below are unaffected.
logging.getLogger("httpx").setLevel(logging.WARNING)

ORIGIN = "IAD"
DESTINATION = "FCO"
CABINS = ["business"]
WANTED_CABINS = {"business"}
WINDOW_START_OFFSET_DAYS = 60
WINDOW_END_OFFSET_DAYS = 90

MAX_ALERTS = 3  # hard spam cap for this run (shared across award + cash alerts), regardless of how many results match

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

    config = load_watchlist()  # real min_trip_value_usd / cpp_floors / dedup_ttl_days / cash_baseline_minutes
    today = datetime.date.today()
    start = today + datetime.timedelta(days=WINDOW_START_OFFSET_DAYS)
    end = today + datetime.timedelta(days=WINDOW_END_OFFSET_DAYS)

    logger.info(
        "Cached Search: origin=%s destination=%s cabins=%s dates=%s..%s (real API call #1 of at most %d)",
        ORIGIN, DESTINATION, CABINS, start.isoformat(), end.isoformat(), 1 + MAX_ALERTS,
    )

    client = SeatsAeroClient(seats_api_key, max_retries=0)
    cash_client = SerpApiClient(serpapi_key, max_retries=0)
    notifier = DiscordNotifier(discord_webhook_url)
    state = FileStateStore(STATE_FILE_PATH)

    try:
        try:
            hits = client.cached_search(ORIGIN, [DESTINATION], start, end, CABINS)
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

        logger.info("X-RateLimit-Remaining: %s", client.last_rate_limit_remaining)
        logger.info("Cached Search returned %d business-cabin result(s)", len(hits))

        if not hits:
            logger.info(
                "No business availability for %s->%s in this window -- this is a normal, valid "
                "result, not an error. Try widening WINDOW_START/END_OFFSET_DAYS or a different "
                "route before concluding anything is broken.",
                ORIGIN, DESTINATION,
            )
            return 0

        sent = 0
        cash_sent = 0
        skipped_duplicate = 0
        skipped_other = 0

        for award in hits:
            if sent >= MAX_ALERTS:
                logger.info("MAX_ALERTS (%d) reached -- spam guard stopping further sends this run", MAX_ALERTS)
                break

            if not passes_award_prefilter(award, WANTED_CABINS):
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
                logger.info("no cash data available for %s -- falling back to cabin-match-only (v1.0-style)", cash_update.key)

            verdict = is_high_value(award, config.awards, WANTED_CABINS, comparable_cash_usd=comparable_cash_usd, taxes_usd=award.taxes_usd)

            # --- independent cash-drop trigger, piggybacking on the SAME
            # baseline refresh above -- see src/poller.py's poll_route() for
            # the identical shape. Never fires on a route's first-ever
            # observation (previous is None -- seeded silently).
            if cash_update.refreshed and cash_update.previous is not None and cash_update.current_fare is not None:
                cash_verdict = is_cash_price_drop(cash_update.current_fare.price_usd, cash_update.previous, config.cash)
                if cash_verdict.fire:
                    c_key = cash_key(cash_update.current_fare)
                    if state.already_alerted(c_key):
                        logger.info("SKIP cash drop %s: already alerted previously (duplicate)", c_key)
                        skipped_duplicate += 1
                    elif sent >= MAX_ALERTS:
                        logger.info("SKIP cash drop %s: MAX_ALERTS (%d) reached", c_key, MAX_ALERTS)
                    else:
                        try:
                            notifier.send_cash_alert(cash_update.current_fare, cash_verdict, cash_update.previous)
                        except DiscordError as exc:
                            logger.error("DISCORD SEND FAILED for cash drop %s: %s", c_key, exc)
                            return 1
                        state.record_alert(c_key, ttl_seconds=config.alerts.dedup_ttl_days * 86400)
                        sent += 1
                        cash_sent += 1
                        logger.info("SENT cash drop %s: $%.0f (%s)", c_key, cash_update.current_fare.price_usd, cash_verdict.reason)

            if not verdict.fire:
                logger.info("SKIP %s: %s", award.availability_id, verdict.reason)
                skipped_other += 1
                continue

            key = award_key(award)
            if state.already_alerted(key):
                logger.info("SKIP %s: already alerted previously (duplicate), key=%s", award.availability_id, key)
                skipped_duplicate += 1
                continue

            if sent >= MAX_ALERTS:
                logger.info("SKIP %s: MAX_ALERTS (%d) reached", award.availability_id, MAX_ALERTS)
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

            real_verdict = is_high_value(
                award, config.awards, WANTED_CABINS,
                comparable_cash_usd=comparable_cash_usd, taxes_usd=parse_trip_taxes_usd(trip),
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
            "Done. %d candidate(s) seen, %d sent (%d cash), %d skipped-as-duplicate, %d skipped-other.",
            len(hits), sent, cash_sent, skipped_duplicate, skipped_other,
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
