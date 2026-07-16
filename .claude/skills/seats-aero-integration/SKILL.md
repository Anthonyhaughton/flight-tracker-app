---
name: seats-aero-integration
description: Integrate with the seats.aero Pro Partner API to fetch award (points/miles) flight availability across airline alliances. Use this whenever the task involves award space, award availability, points/miles redemptions, "saver" awards, mileage programs, or querying seats.aero — including the cached-vs-live search strategy, rate-limit handling, and normalizing availability across Star Alliance / oneworld / SkyTeam. Reach for this skill even if the user just says "check award seats" or "find business-class award space" without naming seats.aero.
---

# seats.aero integration

seats.aero is the highest-leverage source in this project: it already crawls award
availability across 20+ loyalty programs and all three alliances, and exposes it as a
JSON Partner API included with a Pro subscription (personal, non-commercial use only).
We never try to scrape award space ourselves — that problem is solved here.

**Before writing endpoint code, fetch the current reference.** Endpoints and fields drift.
Pull `https://developers.seats.aero/` and its machine index at
`https://developers.seats.aero/llms.txt` (an index of all pages in Markdown plus the
OpenAPI spec) and confirm the exact paths, query params, and response fields against what
this skill describes. Treat this skill as the *design* and the live docs as the *contract*.

## Auth

The key comes from `SEATS_AERO_API_KEY` (loaded via `secrets.py`, never hardcoded). It is
sent as a request header — confirm the exact header name in the live reference. API access
requires an eligible Pro account and is region-restricted; if the key 403s, surface a clear
error telling the owner to confirm API access is enabled on their account, don't retry-loop.

## The cached-vs-live pattern (this is the core strategy)

seats.aero exposes two conceptually different reads. Use them for different jobs:

- **Cached / bulk availability** — cheap on quota, returns pre-crawled availability across
  large date ranges and regions. This is what the poller hits on every scheduled run.
- **Live search** — fresher, queries the mileage program more directly, and is more
  rate-limited. Spend this *only* to confirm a promising hit immediately before alerting,
  so the owner never gets pinged on stale space that's already gone.

Flow inside the poller:

```
for each watched route/date-window:
    hits = seats_aero.cached_search(...)          # cheap, frequent
    candidates = [h for h in hits if passes_prefilter(h)]   # saver + cabin + program
    for c in candidates:
        if valuation.is_high_value(c) and not state.already_alerted(c):
            confirmed = seats_aero.live_search(c)  # spend quota only here
            if confirmed:
                notify(confirmed); state.record(c)
```

## Rate limits

There is a **daily quota that resets at 00:00 UTC** with no manual override. Design for it:

- Poll cached search on a sane cadence (every 15–30 min is plenty for award space).
- Reserve live searches for confirmation only — they are the scarce resource.
- On HTTP 429, back off exponentially and stop for the current run; do not burn the rest
  of the daily quota retrying. Log remaining-quota headers if the API returns them.
- Stagger routes across runs rather than firing every route every minute.

## Normalization: track by program, not by airline

The same physical seat (e.g., an ANA or Qatar flight) appears under multiple programs at
different mileage prices — that is the whole point of alliance award booking. seats.aero
returns availability keyed by **mileage program**, so the same flight can legitimately show
up several times. Preserve the program on every record; it is essential for valuation (CPP
differs per program) and for the dedup key. Never collapse programs together.

## Fields to preserve on each availability record

Confirm exact names against the live schema, but the poller needs at least:

- route (origin, destination), flight date
- **mileage program** (e.g., Aeroplan, Flying Blue, AAdvantage, Virgin Atlantic)
- **cabin** (economy / premium / business / first)
- **mileage cost** and any cash **taxes/fees** portion
- **fare type: saver vs standard/dynamic** — critical. We generally alert on saver only;
  dynamically-priced awards are usually poor value.
- remaining seats / availability count
- whether it's a direct flight
- a stable identifier for the trip (for the live-confirm call and dedup)

## Client shape

Keep the client thin and typed. Return domain objects, not raw dicts, so valuation and
dedup don't depend on seats.aero's wire format.

```python
@dataclass(frozen=True)
class AwardAvailability:
    origin: str
    destination: str
    date: datetime.date
    program: str
    cabin: str
    miles: int
    taxes_usd: float
    is_saver: bool
    direct: bool
    seats: int | None
    trip_id: str

class SeatsAeroClient:
    def cached_search(self, origin, destinations, start, end, cabins) -> list[AwardAvailability]: ...
    def live_search(self, trip_id: str) -> AwardAvailability | None: ...
```

## Don't

- Don't scrape seats.aero's website HTML — use the API you're paying for.
- Don't use it commercially or for anyone but the owner (violates their terms).
- Don't ignore the saver flag; alerting on standard/dynamic awards trains the owner to
  distrust the bot.
