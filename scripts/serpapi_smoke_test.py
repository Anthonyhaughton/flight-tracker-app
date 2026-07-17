#!/usr/bin/env python3
"""One-off live smoke test against the real SerpApi Google Flights engine.

Makes a SINGLE real search call -- one route, one cabin, one concrete date
-- so real data can be eyeballed against what
src/providers/cash/serpapi.py's _parse_itinerary assumes, before this
account's key is ever wired into the poller. No baseline/state writes, no
Notifier calls.

Not wired into the poller or scheduler. Run manually:

    python scripts/serpapi_smoke_test.py

Reads SERPAPI_KEY from the environment (falling back to a minimal .env
loader below so you don't have to `export` it by hand). Never prints the
key or any request header/query string containing it.
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
from src.providers.cash.serpapi import (  # noqa: E402
    SerpApiAuthError,
    SerpApiClient,
    SerpApiRateLimitError,
    _parse_itinerary,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("serpapi_smoke_test")

# httpx logs "HTTP Request: <method> <full URL> ..." at INFO by default,
# which would include api_key=... in the query string for SerpApi (unlike
# seats.aero, whose key is a header). Silence it -- same reasoning as
# scripts/dry_run.py's httpx logger fix.
logging.getLogger("httpx").setLevel(logging.WARNING)

# One route, one cabin, one concrete date, business cabin -- deliberately
# narrow to burn exactly one real SerpApi search.
ORIGIN = "IAD"
DESTINATION = "FCO"
CABIN = "business"
WINDOW_START_OFFSET_DAYS = 60  # ~2 months out


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
        api_key = secrets.get_serpapi_key()
    except secrets.MissingSecretError as exc:
        logger.error(str(exc))
        return 1

    date = datetime.date.today() + datetime.timedelta(days=WINDOW_START_OFFSET_DAYS)

    logger.info(
        "Google Flights search: departure_id=%s arrival_id=%s outbound_date=%s type=2 (one-way) travel_class=business",
        ORIGIN, DESTINATION, date.isoformat(),
    )

    # max_retries=0: a single call attempt, no automatic retry-on-429, so a
    # parsing bug or a quota hit can't silently burn a second call.
    client = SerpApiClient(api_key, max_retries=0)
    try:
        # Reaching into _search_one/_get deliberately, rather than going
        # through the public search(): we need the raw JSON for the
        # side-by-side comparison this check is for, and running the exact
        # same _parse_itinerary the poller will use (not a reimplementation)
        # is the whole point.
        data = client._get(
            {
                "engine": "google_flights",
                "api_key": api_key,
                "departure_id": ORIGIN,
                "arrival_id": DESTINATION,
                "outbound_date": date.isoformat(),
                "type": "2",
                "travel_class": "3",
                "currency": "USD",
                "hl": "en",
                "gl": "us",
            }
        )
    except SerpApiAuthError:
        logger.error(
            "AUTH FAILED (401): SerpApi rejected the key. Check https://serpapi.com/manage-api-key."
        )
        return 1
    except SerpApiRateLimitError:
        logger.error(
            "RATE LIMITED / QUOTA EXHAUSTED (429): check remaining searches at "
            "https://serpapi.com/account -- nothing to do but wait or upgrade the plan."
        )
        return 1
    finally:
        client.close()

    status = (data.get("search_metadata") or {}).get("status")
    logger.info("search_metadata.status=%s", status)

    if status == "Error":
        logger.error("SerpApi returned a query-level error: %s", data.get("error"))
        print("\n=== RAW response ===")
        print(json.dumps(data, indent=2))
        return 1

    candidates = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    logger.info(
        "Found %d candidate itinerar(y/ies) (%d best_flights + %d other_flights)",
        len(candidates), len(data.get("best_flights") or []), len(data.get("other_flights") or []),
    )

    if not candidates:
        logger.info(
            "No flights found for %s->%s on %s -- a normal, valid result, not an error. "
            "Try a different date or route to see real data parse.",
            ORIGIN, DESTINATION, date,
        )
        return 0

    non_one_way = [c for c in candidates if c.get("type", "One way") != "One way"]
    if non_one_way:
        logger.warning(
            "%d of %d candidate(s) are NOT type=One way despite a type=2 request -- "
            "these would be discarded by _search_one's directionality guard.",
            len(non_one_way), len(candidates),
        )

    cheapest = min(candidates, key=lambda c: c["price"])

    print("\n=== RAW (cheapest candidate) ===")
    print(json.dumps(cheapest, indent=2))

    try:
        parsed = _parse_itinerary(cheapest, ORIGIN, DESTINATION, date, CABIN)
    except Exception as exc:
        logger.error("PARSING FAILED on the cheapest candidate -- _parse_itinerary choked on real data: %r", exc)
        return 1

    print("\n=== PARSED (CashFare) ===")
    print(parsed)

    if parsed.return_date is not None:
        logger.error(
            "DIRECTIONALITY BUG: parsed CashFare has a non-None return_date (%s) from a type=2 "
            "one-way request -- this would silently double the CPP denominator.",
            parsed.return_date,
        )
        return 1

    logger.info("Parsed cleanly. return_date=None confirmed (one-way, as requested).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
