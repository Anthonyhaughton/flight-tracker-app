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

## Baseline tracking (this is what makes "price drop" meaningful)

A price is only a "deal" relative to something. Maintain a rolling baseline per
route+cabin+date-bucket in the state store (see `deal-valuation` for the store):

- On each poll, record the observed low fare.
- Keep a trailing baseline (e.g., median or 30-day low). A simple, robust default: store
  the trailing minimum and a timestamp, plus an EMA for "typical" price.
- A cash alert fires when the current low drops meaningfully below baseline (percent or
  absolute threshold from `watchlist.yaml`) AND clears the dedup check.

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
