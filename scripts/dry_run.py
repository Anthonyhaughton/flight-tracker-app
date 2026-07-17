#!/usr/bin/env python3
"""First end-to-end dry run: one real seats.aero Cached Search -> valuation
gate -> Get Trips -> a real Discord embed in your actual channel.

Exercises the SAME real components the poller uses (SeatsAeroClient,
is_high_value, award_key, DiscordNotifier) for exactly one route
(IAD -> FCO, business), rather than calling poll_route() directly, so this
script can enforce its own hard spam cap and give explicit per-failure-mode
messages -- neither of which poll_route() has, by design (production doesn't
want a silent alert cap, and DOES want auth/quota errors to propagate loudly
rather than being swallowed with a friendly message).

Real API call budget: exactly 1 Cached Search call, plus up to MAX_ALERTS
Get Trips calls (one per candidate that clears the gate and isn't a dedup
duplicate) -- so at most 1 + MAX_ALERTS seats.aero calls total. Get Trips is
not optional here: it's the only place real MileageCost/TotalTaxes/Cabin
come from, and the whole point of this run is to see a real Discord embed
built from real data.

Not wired into the poller or scheduler. Run manually:

    python scripts/dry_run.py

Dedup state persists to scripts/.dry_run_state.json (gitignored) across runs
of this script specifically -- production uses DynamoDB (src/state.py); this
is a local stand-in so a second run of this script doesn't re-alert the same
deal, without needing an AWS account set up yet.
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
from src.config import load_watchlist  # noqa: E402
from src.notify.discord import DiscordError, DiscordNotifier  # noqa: E402
from src.providers.seats_aero import (  # noqa: E402
    SeatsAeroAuthError,
    SeatsAeroClient,
    SeatsAeroRateLimitError,
    parse_trip_taxes_usd,
    select_trip_for_cabin,
)
from src.state import Baseline, award_key  # noqa: E402
from src.valuation import is_high_value  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("dry_run")

# httpx logs "HTTP Request: <method> <full URL> ..." at INFO by default. That's
# harmless for seats.aero (key is a header, not part of the URL) but NOT for
# Discord webhooks -- the webhook URL itself is the credential, so logging it
# would leak it to stdout/logs. Silence httpx's own request logger; our
# explicit logger.info() calls below are unaffected.
logging.getLogger("httpx").setLevel(logging.WARNING)

ORIGIN = "IAD"
DESTINATION = "FCO"
CABINS = ["business"]
WANTED_CABINS = {"business"}
WINDOW_START_OFFSET_DAYS = 60
WINDOW_END_OFFSET_DAYS = 90

MAX_ALERTS = 3  # hard spam cap for this run, regardless of how many results match

STATE_FILE_PATH = Path(__file__).resolve().parent / ".dry_run_state.json"


class FileStateStore:
    """Local, JSON-file-backed StateStore for this script only. Production
    uses DynamoDB (src/state.py) -- this exists purely so a second run of
    this script doesn't re-alert a deal it already sent, without requiring
    an AWS account to be set up yet."""

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
        return Baseline(price_usd=item["price_usd"], updated_at=datetime.datetime.fromisoformat(item["updated_at"]))

    def update_baseline(self, route_key: str, price: float) -> None:
        self._data["baselines"][route_key] = {
            "price_usd": price,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
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
        discord_webhook_url = secrets.get_discord_webhook_url()
    except secrets.MissingSecretError as exc:
        logger.error(str(exc))
        return 1

    config = load_watchlist()  # real min_trip_value_usd / cpp_floors / dedup_ttl_days
    today = datetime.date.today()
    start = today + datetime.timedelta(days=WINDOW_START_OFFSET_DAYS)
    end = today + datetime.timedelta(days=WINDOW_END_OFFSET_DAYS)

    logger.info(
        "Cached Search: origin=%s destination=%s cabins=%s dates=%s..%s (real API call #1 of at most %d)",
        ORIGIN, DESTINATION, CABINS, start.isoformat(), end.isoformat(), 1 + MAX_ALERTS,
    )

    client = SeatsAeroClient(seats_api_key, max_retries=0)
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
        skipped_duplicate = 0
        skipped_other = 0

        for award in hits:
            if sent >= MAX_ALERTS:
                logger.info("MAX_ALERTS (%d) reached -- spam guard stopping further sends this run", MAX_ALERTS)
                break

            verdict = is_high_value(award, config.awards, WANTED_CABINS, taxes_usd=award.taxes_usd)
            if not verdict.fire:
                logger.info("SKIP %s: %s", award.availability_id, verdict.reason)
                skipped_other += 1
                continue

            key = award_key(award)
            if state.already_alerted(key):
                logger.info("SKIP %s: already alerted previously (duplicate), key=%s", award.availability_id, key)
                skipped_duplicate += 1
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

            real_verdict = is_high_value(award, config.awards, WANTED_CABINS, taxes_usd=parse_trip_taxes_usd(trip))
            if not real_verdict.fire:
                logger.info("SKIP %s: failed real-taxes recheck (%s)", award.availability_id, real_verdict.reason)
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
            "Done. %d candidate(s) seen, %d sent, %d skipped-as-duplicate, %d skipped-other.",
            len(hits), sent, skipped_duplicate, skipped_other,
        )
        if sent > 0:
            logger.info("Check Discord -- %d embed(s) should have landed in the channel.", sent)
        else:
            logger.info("Nothing sent this run.")
        return 0
    finally:
        client.close()
        notifier.close()


if __name__ == "__main__":
    sys.exit(main())
