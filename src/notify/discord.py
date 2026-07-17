"""Discord webhook notifier: sends award alerts as rich embeds.

Not spamming lives in valuation.py's dedup; this module only formats and
sends. Default notifier for v1.0 (see watchlist.yaml's `notifier` key) --
Telegram remains available as a swappable alternate impl behind the same
Notifier interface (src/notify/base.py).

No MarkdownV2-style escaping needed here -- Discord embeds render field
values as plain text, not a markup language with reserved characters like
Telegram's parse_mode=MarkdownV2.
"""

from __future__ import annotations

import datetime
import logging
import time

import httpx

from src.providers.seats_aero import AwardAvailability, parse_trip_taxes_usd
from src.valuation import Verdict

logger = logging.getLogger(__name__)

# Discord's documented embed limits -- violating any of these gets a 400
# from the real webhook, so we check locally before ever sending.
MAX_TITLE_LEN = 256
MAX_FIELD_NAME_LEN = 256
MAX_FIELD_VALUE_LEN = 1024
MAX_TOTAL_EMBED_CHARS = 6000
MAX_FIELDS = 25
MAX_EMBEDS = 10

COLOR_GOOD_DEAL = 0x2ECC71  # green


class DiscordError(RuntimeError):
    pass


class DiscordValidationError(DiscordError):
    """Raised before ever sending, when a built embed violates Discord's
    documented limits -- a formatting bug must not spam a malformed
    payload at the real webhook (which would just 400 anyway)."""


class DiscordNotifier:
    def __init__(self, webhook_url: str, *, client: httpx.Client | None = None, max_retries: int = 1):
        self._webhook_url = webhook_url
        self._client = client or httpx.Client(timeout=10.0)
        self._max_retries = max_retries

    def send_award_alert(
        self, award: AwardAvailability, verdict: Verdict, trip: dict, *, deep_link: str | None = None
    ) -> None:
        embed = format_award_embed(award, verdict, trip, deep_link=deep_link)
        self._send_embeds([embed])

    def close(self) -> None:
        self._client.close()

    def _send_embeds(self, embeds: list[dict]) -> None:
        embeds = _validate_and_clean_embeds(embeds)
        payload = {"embeds": embeds}

        attempt = 0
        while True:
            response = self._client.post(self._webhook_url, json=payload)

            if response.status_code == 204:
                return

            if response.status_code == 429:
                if attempt >= self._max_retries:
                    raise DiscordError(
                        f"Discord webhook rate limited (429) and retry budget exhausted: {response.text}"
                    )
                retry_after = _parse_retry_after(response)
                logger.warning("Discord webhook rate limited (429); retrying after %.2fs", retry_after)
                time.sleep(retry_after)
                attempt += 1
                continue

            logger.error("Discord webhook send failed (%d): %s", response.status_code, response.text)
            raise DiscordError(f"Discord webhook send failed ({response.status_code}): {response.text}")


def _parse_retry_after(response: httpx.Response) -> float:
    """Discord's 429 body includes a `retry_after` (seconds, float);
    fall back to the standard Retry-After header, then a conservative
    default if neither is present or parseable."""
    try:
        body = response.json()
        if "retry_after" in body:
            return float(body["retry_after"])
    except ValueError:
        pass
    header = response.headers.get("Retry-After")
    if header is not None:
        try:
            return float(header)
        except ValueError:
            pass
    return 1.0


def _validate_and_clean_embeds(embeds: list[dict]) -> list[dict]:
    if not isinstance(embeds, list):
        raise DiscordValidationError("embeds payload must be a list")
    if len(embeds) > MAX_EMBEDS:
        raise DiscordValidationError(f"too many embeds ({len(embeds)} > {MAX_EMBEDS})")

    cleaned = []
    for embed in embeds:
        embed = dict(embed)  # don't mutate the caller's dict
        # Discord 400s on a field with an empty/missing value -- drop those
        # rather than send a payload we already know will be rejected.
        fields = [f for f in embed.get("fields", []) if f.get("value")]
        embed["fields"] = fields

        title = embed.get("title") or ""
        if len(title) > MAX_TITLE_LEN:
            raise DiscordValidationError(f"embed title exceeds {MAX_TITLE_LEN} chars: {title!r}")

        if len(fields) > MAX_FIELDS:
            raise DiscordValidationError(f"too many fields ({len(fields)} > {MAX_FIELDS})")

        total_chars = len(title) + len(embed.get("description") or "")
        total_chars += len((embed.get("footer") or {}).get("text") or "")

        for field in fields:
            name, value = field.get("name", ""), field["value"]
            if len(name) > MAX_FIELD_NAME_LEN:
                raise DiscordValidationError(f"field name exceeds {MAX_FIELD_NAME_LEN} chars: {name!r}")
            if len(value) > MAX_FIELD_VALUE_LEN:
                raise DiscordValidationError(f"field {name!r} value exceeds {MAX_FIELD_VALUE_LEN} chars")
            total_chars += len(name) + len(value)

        if total_chars > MAX_TOTAL_EMBED_CHARS:
            raise DiscordValidationError(f"embed total chars ({total_chars}) exceeds {MAX_TOTAL_EMBED_CHARS}")

        cleaned.append(embed)
    return cleaned


def format_award_embed(award: AwardAvailability, verdict: Verdict, trip: dict, *, deep_link: str | None = None) -> dict:
    """`trip` is one entry from SeatsAeroClient.get_trips(award.availability_id),
    already filtered to award.cabin by select_trip_for_cabin() in poller.py
    (Get Trips returns itineraries across ALL cabins on the availability, so
    trip["Cabin"] can't be trusted blindly -- see select_trip_for_cabin's
    docstring). The title uses award.cabin directly, not trip's own Cabin
    field, since award.cabin is what Cached Search and the valuation gate
    actually matched on.

    No "saver" in the title: there's no per-item saver flag on the wire --
    saver-equivalence comes entirely from the Cached Search request-time
    filter (see seats_aero.py's cached_search docstring), so claiming it
    per-item here would be asserting something we can't verify."""
    cabin_label = award.cabin.title()
    program_label = award.program.replace("_", " ").title()
    miles = int(trip["MileageCost"])
    taxes_usd = parse_trip_taxes_usd(trip)
    seats = trip.get("RemainingSeats")

    fields = [
        {"name": "Program", "value": program_label, "inline": True},
        {"name": "Date", "value": award.date.isoformat(), "inline": True},
        {"name": "Miles", "value": f"{miles:,}", "inline": True},
        {"name": "Taxes (USD)", "value": f"${taxes_usd:,.2f}", "inline": True},
        {"name": "Seats", "value": str(seats) if seats is not None else None, "inline": True},
        {"name": "Nonstop", "value": "Yes" if award.direct else "No", "inline": True},
    ]
    fields = [f for f in fields if f["value"]]  # Discord 400s on empty field values

    embed: dict = {
        "title": f"{cabin_label} - {award.origin} -> {award.destination}",
        "color": COLOR_GOOD_DEAL,
        "fields": fields,
        "footer": {"text": verdict.headline or "Award deal alert"},
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if deep_link:
        embed["url"] = deep_link
    return embed
