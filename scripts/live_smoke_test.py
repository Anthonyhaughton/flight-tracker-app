#!/usr/bin/env python3
"""One-off live smoke test against the real seats.aero Pro API.

Makes a SINGLE real Cached Search call -- no Get Trips, no live-search (it
doesn't exist on Pro anyway), and absolutely no Telegram/Notifier calls --
so real data can be eyeballed against what providers/seats_aero.py's
_parse_item assumes, before this account's key is ever wired into the poller.

Not wired into the poller or scheduler. Run manually:

    python scripts/live_smoke_test.py

Reads SEATS_AERO_API_KEY from the environment (falling back to a minimal
.env loader below so you don't have to `export` it by hand). Never prints
the key or any request header.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src import secrets  # noqa: E402
from src.providers.seats_aero import (  # noqa: E402
    _CABIN_CODES,
    SeatsAeroAuthError,
    SeatsAeroClient,
    SeatsAeroRateLimitError,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("live_smoke_test")

# One route, one cabin, a small take, a ~2-month-out window -- deliberately
# narrow to burn as few of the 1,000/day calls as possible on a single check.
ORIGIN = "IAD"
DESTINATION = "FCO"
CABINS = ["business"]
# Request-side cabin filter is the full word (see cached_search's docstring);
# the response uses the single-letter Y/W/J/F prefix on each field instead.
CABIN_QUERY = "business"
CABIN_CODE = _CABIN_CODES["business"]
TAKE = 10
WINDOW_START_OFFSET_DAYS = 60
WINDOW_END_OFFSET_DAYS = 90


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so this script works without a separate
    python-dotenv dependency. Never logs or returns values -- only sets
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
        api_key = secrets.get_seats_aero_api_key()
    except secrets.MissingSecretError as exc:
        logger.error(str(exc))
        return 1

    today = datetime.date.today()
    start = today + datetime.timedelta(days=WINDOW_START_OFFSET_DAYS)
    end = today + datetime.timedelta(days=WINDOW_END_OFFSET_DAYS)

    logger.info(
        "Cached Search: origin=%s destination=%s cabin=%s dates=%s..%s take=%d",
        ORIGIN, DESTINATION, CABIN_QUERY, start.isoformat(), end.isoformat(), TAKE,
    )

    # max_retries=0: a single call attempt, no automatic retry-on-429, so a
    # parsing bug or a quota hit can't silently burn a second call.
    client = SeatsAeroClient(api_key, max_retries=0)
    try:
        # Reaching into _get/_parse_item deliberately, rather than going
        # through the public cached_search(): we need the raw JSON for the
        # side-by-side comparison the task asked for, and running the exact
        # same _parse_item the poller will use (not a reimplementation) is
        # the whole point of this check.
        payload = client._get(
            "/search",
            {
                "origin_airport": ORIGIN,
                "destination_airport": DESTINATION,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "cabins": CABIN_QUERY,
                "take": TAKE,
            },
        )
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
    finally:
        if client.last_rate_limit_remaining is not None:
            logger.info("X-RateLimit-Remaining: %s", client.last_rate_limit_remaining)
        client.close()

    raw_items = payload.get("data", [])
    logger.info("Cached Search returned %d raw item(s), hasMore=%s", len(raw_items), payload.get("hasMore"))

    if not raw_items:
        logger.info(
            "No availability for %s->%s in this window -- a normal, valid result, not an error. "
            "Try a wider date window or a different route to see real data parse.",
            ORIGIN, DESTINATION,
        )
        return 0

    # Print raw vs. parsed for the first result, side by side, so field
    # mapping can be eyeballed against real data.
    print("\n=== RAW (first result) ===")
    print(json.dumps(raw_items[0], indent=2))

    try:
        first_parsed = client._parse_item(raw_items[0], CABINS)
    except Exception as exc:
        logger.error("PARSING FAILED on the first raw record -- _parse_item choked on real data: %r", exc)
        print("\n--- offending raw record ---")
        print(json.dumps(raw_items[0], indent=2))
        return 1

    print("\n=== PARSED (first result, AwardAvailability) ===")
    if not first_parsed:
        print(f"(no {CABINS[0]} availability on this specific record -- {CABIN_CODE}Available was false)")
    for award in first_parsed:
        print(award)

    # Parse every remaining raw item too, so a field assumption that only
    # breaks on item #7 doesn't slip through unnoticed.
    failures: list[tuple[int, dict, Exception]] = []
    for i, item in enumerate(raw_items[1:], start=1):
        try:
            client._parse_item(item, CABINS)
        except Exception as exc:
            failures.append((i, item, exc))

    if failures:
        logger.error("PARSING FAILED on %d of the remaining %d raw item(s):", len(failures), len(raw_items) - 1)
        for i, item, exc in failures:
            print(f"\n--- item[{i}] failed: {exc!r} ---")
            print(json.dumps(item, indent=2))
        return 1

    logger.info("All %d raw item(s) parsed cleanly through _parse_item.", len(raw_items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
