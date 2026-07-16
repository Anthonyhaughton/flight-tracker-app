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

## Cached Search + Get Trips (NOT Live Search — read this before coding)

**Live Search is not available on Pro accounts, full stop** — per seats.aero's own docs,
"Live Search is not available to Pro users, regardless of use case. Access is limited to
approved commercial partners." Since this project runs on a personal Pro subscription, do
not design around Live Search and do not implement a `live_search()` call. An earlier
version of this skill assumed otherwise; that was wrong and has been corrected.

The two endpoints actually available on Pro are:

- **Cached Search** (`GET /search`) — the main endpoint. Query specific origin/destination
  airports and a date range across all mileage programs at once. This is what the poller
  hits on every scheduled run. Pre-crawled, so there's some staleness, but it's re-crawled
  regularly and is the only practical option at this tier.
- **Get Trips** (on an `Availability.ID` from a Cached Search hit, or via
  `include_trips=true` on the search call itself) — returns the underlying flight-level
  detail: real flight numbers, per-trip `RemainingSeats`, `MileageCost` (int, not string),
  `TotalTaxes`, `Cabin`, routing/times. Use this **right before alerting** on a promising
  Cached Search hit as a freshness/detail check — it's the Pro-accessible substitute for
  the live-confirm step we can't have. It's not a live re-query of the airline, so treat it
  as "more specific," not "guaranteed current."

Flow inside the poller:

```
for each watched route/date-window:
    hits = seats_aero.cached_search(...)                    # cheap, frequent
    candidates = [h for h in hits if passes_prefilter(h)]    # cabin/program/threshold
    for c in candidates:
        if valuation.is_high_value(c) and not state.already_alerted(c):
            detail = seats_aero.get_trips(c.availability_id)  # richer detail, still Pro-tier
            if detail:
                notify(c, detail); state.record(c)
```

There's a `Bulk Availability` endpoint too (one program, broad date/region range) — useful
later for "anywhere in Europe" style v2 inspiration search, not needed for v1.0's specific
routes.

## Rate limits

Pro API access is **1,000 calls per calendar day**, resetting at 00:00 UTC, with no manual
override — this is a usage-based daily cap, not a per-minute rate limit. Every response
includes an `X-RateLimit-Remaining` header; log it. Design for it:

- Poll cached search on a sane cadence (every 15–30 min is plenty for award space).
- Reserve Get Trips calls for confirmation right before alerting — they're extra calls
  against the same daily budget, so don't call it for every candidate on every poll, only
  ones that already cleared the valuation gate.
- On HTTP 429 (quota exhausted), stop for the current run rather than retrying — there is
  no reset until midnight UTC regardless of backoff.
- Stagger routes across runs rather than firing every route every minute.
- **API access itself isn't guaranteed** even on Pro — it's gated per-account ("not all Pro
  users will see API access enabled") and may be region-restricted. If the account has no
  "API" tab under Settings, there is no key to generate; that's a signup-time check, not a
  code bug.

## Normalization: track by program, not by airline

The same physical seat (e.g., an ANA or Qatar flight) appears under multiple programs at
different mileage prices — that is the whole point of alliance award booking. seats.aero
calls each mileage program a **source** (e.g. `aeroplan`, `flyingblue`, `united`,
`virginatlantic` — the full list is in the API's Concepts doc) and returns availability
keyed by source, so the same flight can legitimately show up several times. Preserve the
source/program on every record; it is essential for valuation (CPP differs per program) and
for the dedup key. Never collapse programs together. Note also: not every program reports
seat counts or trip-level data (the Concepts doc has a per-source capability table) — treat
missing seat counts as "unknown," not zero-and-skip.

## The real Cached Search response schema (verified against live docs)

**There is no explicit "saver" boolean field anywhere in this schema.** Saver-vs-dynamic
pricing is controlled at *request time* by the `include_filtered` query param: leave it
false/omitted and the API returns dynamic-price-filtered (effectively saver-equivalent)
results; set it true only if you deliberately want raw/unfiltered results too. Do not invent
or look for a per-item saver flag — gate on the request param, not a response field.

Also note the request param for cabin filtering is `cabins` (plural, comma-separated full
words like `business,first`), NOT `cabin` and NOT the single-letter Y/W/J/F codes. The
single letters are a *response-field prefix* convention only; sending `cabin=J` returns a
400. (Verified against the live API — it was a real bug.)

Each Availability object is flat, one row per route+date+program, with **per-cabin-letter**
fields (`Y`=economy, `W`=premium economy, `J`=business, `F`=first):

```json
{
  "ID": "2QSaUXJ0ZuSVqgrRWqkSlXhnVbS",
  "RouteID": "2HmSwbzAS9SnEdtIsf3nkjozpX1",
  "Route": {
    "OriginAirport": "SFO", "OriginRegion": "North America",
    "DestinationAirport": "JFK", "DestinationRegion": "North America",
    "Distance": 2582, "Source": "american"
  },
  "Date": "2023-08-11", "ParsedDate": "2023-08-11T00:00:00Z",
  "JAvailable": true, "JMileageCost": "33000", "JTotalTaxes": 5030,
  "JRemainingSeats": 0, "JAirlines": "AA, B6", "JDirect": true,
  "FAvailable": true, "FMileageCost": "33000", "FTotalTaxes": 5030,
  "FAirlines": "AA", "FDirect": true,
  "Source": "american", "CreatedAt": "...", "UpdatedAt": "...",
  "AvailabilityTrips": null
}
```

Type gotchas (both verified against real responses — do not conflate them):
- **`{X}MileageCost` is a string** (`"33000"`) — cast to `int()`.
- **`{X}TotalTaxes` is an int in cents** (`5030` = $50.30) — divide by 100, don't `int()`-a-string it.
- `{X}Airlines` is a comma-separated string, not a list.

**Taxes ARE available at Cached Search time** via `{X}TotalTaxes` — an earlier version of
this skill wrongly claimed taxes only appear at Get Trips. They don't; you can compute real
effective CPP (miles + taxes) on the initial cheap call, before spending a Get Trips call.

**Critical tax ambiguity (live-verified):** some programs don't report taxes — the Concepts
doc names `qatar`, `turkish`, `singapore`. For these, `{X}TotalTaxes` comes back as `0`,
which is **bit-for-bit identical to a genuine $0 co-pay**. You cannot tell "unknown" from
"really free" from the value alone. Handle it with a known-non-reporting-programs set and
represent unknown taxes as `None`, never `0.0` — treating unknown as zero silently inflates
CPP for exactly those programs. Also treat a missing field as `None` defensively, for any
program.

Get Trips (called on a hit's `ID` right before alerting) still returns the most
*authoritative* taxes — a per-trip `TotalTaxes` (int cents) that can differ from the Cached
Search summary (e.g. per-segment surcharges). Use Cached Search taxes for the first-pass
gate; use Get Trips taxes as the confirming figure right before alerting. Its other typed
fields (`MileageCost` int, `Cabin` string, `RemainingSeats`, `FlightNumbers`,
`DepartsAt`/`ArrivesAt`) are best for the final alert message.

Other real fields present but unused by this project (informational — ignore unless a future
version needs them): a `{X}...Raw` twin of each cabin field (the unfiltered/dynamic-pricing
counterpart — relevant if a v2 wants dynamic pricing without `include_filtered`); a
`{X}Direct...` family (`JDirectMileageCost` etc., nonstop-specific pricing distinct from the
general `{X}MileageCost`); an `OptionalPricing` object (card-specific alternate pricing); and
a redundant `Route.ID` duplicating top-level `RouteID`.

## Client shape

Keep the client thin and typed. Return domain objects, not raw dicts, so valuation and
dedup don't depend on seats.aero's wire format.

```python
@dataclass(frozen=True)
class AwardAvailability:
    origin: str
    destination: str
    date: datetime.date
    program: str          # the "Source" field
    cabin: str             # normalized from Y/W/J/F
    miles: int              # parsed from the {X}MileageCost string
    airlines: list[str]     # parsed from the {X}Airlines comma-string
    direct: bool
    seats: int | None
    availability_id: str    # the "ID" field, used for Get Trips + dedup

class SeatsAeroClient:
    def cached_search(self, origin, destinations, start, end, cabins) -> list[AwardAvailability]: ...
    def get_trips(self, availability_id: str) -> list[dict] | None: ...
```

## Don't

- Don't scrape seats.aero's website HTML — use the API you're paying for.
- Don't use it commercially or for anyone but the owner (violates their terms).
- Don't implement or design around `live_search()` — it's commercial-partner-only and will
  never work on this account.
- Don't look for a saver/standard boolean on the response — it doesn't exist. Control it via
  the `include_filtered` request param instead (see above).
- Don't treat `{X}MileageCost` as a number without casting — it's a string on the wire.
