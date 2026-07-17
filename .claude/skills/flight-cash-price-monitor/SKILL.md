---
name: flight-cash-price-monitor
description: Fetch and monitor cash (revenue) flight fares behind a swappable provider interface, defaulting to a Google-Flights scraping-API wrapper (SerpApi). Use this whenever the task involves cash fares, revenue ticket prices, price drops, fare baselines, "how much does this flight cost", or comparing an award redemption against the cash price. Also use it when deciding how to source cash fare data or when someone proposes scraping Google Flights or airline sites directly — this skill explains the right way to do it. Reach for it even if the user just says "track the price of flights to Rome".
---

# Cash fare monitoring

There is **no official Google Flights API** (Google shut down QPX Express in 2018 and never
replaced it). So cash fares come from a **paid scraping-API wrapper** that returns clean
JSON and handles the headless browser, proxy rotation, and captchas for us. We do **not**
run our own stealth browser fleet against Google or airline sites for the core pipeline —
that is a maintenance treadmill, is usually against those sites' ToS, and Lambda egress IPs
get blocked fast anyway.

## Provider interface (the swap point)

Never let the rest of the codebase know which vendor we use. Everything goes through:

```python
@dataclass(frozen=True)
class CashFare:
    origin: str
    destination: str
    date: datetime.date
    return_date: datetime.date | None
    cabin: str
    price_usd: float
    airline: str
    stops: int
    deep_link: str | None      # booking URL when the provider supplies one

class CashFareProvider(Protocol):
    def search(
        self,
        origin: str,
        destinations: list[str],
        start: datetime.date,
        end: datetime.date,
        cabin: str,
    ) -> list[CashFare]: ...
```

Default implementation: **SerpApi Google Flights** (`providers/cash/serpapi.py`). Reasonable
alternatives if the owner prefers: Scrapfly or Zyte (also scraping-API wrappers), or Duffel
if live *bookable* NDC fares are wanted — but note Duffel is booking-oriented and bills an
excess search fee past a search-to-book ratio, so a pure monitor that never books can get
expensive on it. Keep those as alternate classes behind the same interface.

**Fetch the provider's current docs before coding the impl** — query params, result shape,
and pricing tiers change. Read the key from `SERPAPI_KEY` via `secrets.py`.

`deep_link` is frequently `None` in practice — this is a deliberate tradeoff, not a bug.
SerpApi's Google Flights engine only returns an opaque `booking_token`/`departure_token` per
itinerary; resolving that into an actual bookable URL costs a **second, separately-billed**
SerpApi request per itinerary. This project already spends one exact-date confirm call per
finalist (see `deal-valuation`) — resolving deep links too would double that cost for a
"nice to have" click-through. Default to leaving `deep_link=None` and let the alert stand on
its own route/price/date; only resolve it if a later version decides the extra spend is
worth it.

## Directionality: one-way, always — real bug risk, not theoretical

seats.aero award costs are **one-way** (a Cached Search hit prices a single direction).
Every cash search this project makes MUST also be one-way — Google Flights (and SerpApi's
wrapper around it) defaults to round-trip. Get this wrong and every effective-CPP number in
`deal-valuation` is silently wrong (a round-trip price compared against a one-way award
cost roughly doubles the cash side of the comparison) **without any test ever failing** —
the bug wouldn't look "wrong" in isolation, it would just make every redemption look worse
(or, on the wrong side of a threshold, artificially better) than it really is, and nothing
about a green test suite would catch a directionality mismatch baked into the fixtures
themselves.

Defend against it twice, not once:
1. **Request-side:** always send the provider's one-way request type explicitly (SerpApi's
   Google Flights engine: `type="2"`), and never send a return date.
2. **Response-side:** don't just trust that the request param did what you asked — check
   the response's own per-itinerary type/trip-type field too, and discard (log + skip,
   don't crash) any result that isn't actually one-way. A future provider API change or a
   request-param typo should not be able to silently reintroduce this.

Test both layers explicitly: assert the request never carries a return date, and assert a
deliberately-cheapest round-trip decoy planted in a mocked response gets discarded rather
than picked (naive "pick the cheapest result" selection logic would otherwise pick exactly
the wrong, unverified one).

## Baseline tracking (this is what makes "price drop" meaningful)

A price is only a "deal" relative to something. Maintain a rolling baseline per
route+cabin+date-bucket in the state store (see `deal-valuation` for the store).
**Implemented bucket granularity: ISO week** (`{origin}-{destination}:{cabin}:{iso_year}-W{iso_week}`)
— nearby travel dates within the same route/cabin share one cached baseline. This is what
actually bounds provider call volume: at most one refresh per route+cabin+week per
`cash_baseline_minutes`, regardless of how many exact days within that week have qualifying
candidates. (Tune the bucket width here if it proves too coarse/fine — week was chosen as a
reasonable default, not derived from data.)

- On each refresh, record the observed low fare (`Baseline.trailing_min_usd` /
  `Baseline.ema_usd` in `src/state.py`).
- Keep a trailing baseline: the trailing minimum (only ever decreases) and an EMA for
  "typical" price (blends each new observation in at a tunable smoothing factor). Both are
  computed by one shared function so every StateStore implementation (in-memory, file-backed,
  DynamoDB) computes them identically and can't drift from each other.
- A cash alert fires when the current low drops meaningfully below the **EMA** baseline
  (percent or absolute threshold from `watchlist.yaml`) AND clears the dedup check — see
  `deal-valuation` for why EMA and not the trailing minimum.
- The weekly-bucketed baseline is accurate enough for this drop-trigger AND for the initial
  CPP prefilter, but NOT accurate enough to be the final number that gates a real award
  alert — see `deal-valuation`'s two-stage cash pricing (weekly estimate, then one real
  exact-date confirm call for finalists).

Don't alert on the first observation of a route (no baseline yet) — seed silently.

## Two jobs cash data does here

1. **Standalone cash deals** — a normal fare falls well below its baseline (e.g., a mistake
   fare or flash sale). Alert on the drop.
2. **Award valuation input** — the *live cash price* for the same route/date is the
   denominator in the effective-cents-per-point calculation in `deal-valuation`. The award
   pipeline calls `search()` to price the comparable cash ticket before deciding if a
   redemption is high value.

## Operational notes

- Cache aggressively. Cash fares don't move second-to-second; an hourly baseline refresh is
  plenty and keeps provider costs down. Respect the provider's own rate limits.
- Normalize currency to USD at the provider boundary.
- Handle empty/blocked responses gracefully — a provider hiccup should log and skip, not
  crash the whole poll run or corrupt the baseline.
- If someone insists on self-scraping a narrow gap the provider can't cover, that is the
  *only* place to consider Playwright + residential proxies + stealth patches — and it must
  be isolated in its own module, run off Lambda (Fargate/VPS with rotating residential
  IPs), and clearly flagged as best-effort. Do not make the core pipeline depend on it.
