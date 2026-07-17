---
name: deal-valuation
description: Decide whether a flight deal (award or cash) is genuinely "high value" and worth an alert, and design the deduplication that prevents alert spam. Use this whenever the task involves cents-per-point (CPP) math, valuing a points/miles redemption, comparing award cost against cash, setting alert thresholds, deciding "is this a good deal", or building the dedup/debounce logic for notifications. This is the brain of the pipeline — reach for it whenever the question is "should we alert on this?" rather than "how do we fetch this?".
---

# Deal valuation & dedup

This skill owns the single decision the whole project exists to make: **is this deal good
enough to interrupt the owner's day, and have we already told them?** Fetching data is easy;
this judgment is where the project succeeds or fails. If it's too loose the owner mutes the
bot; too tight and they miss deals.

## The valuation model

Two triggers, either can fire an alert.

### 1. Award redemption value (effective cents-per-point)

For an award seat, compute how much value each mile extracts versus paying cash:

```
effective_cpp = (comparable_cash_price_usd - award_taxes_fees_usd) / miles_required * 100
```

- `comparable_cash_price_usd` comes from `flight-cash-price-monitor` for the same
  route/date/cabin.
- `award_taxes_fees_usd` is available from the **Cached Search response itself**
  (`{X}TotalTaxes`, cents) — no Get Trips call needed for the first-pass gate. See
  `seats-aero-integration`.
- Alert when **`effective_cpp >= floor_cpp[program]`** AND the cabin is one the owner cares
  about (long-haul business/first is the sweet spot). Saver-equivalence is handled at request
  time, not here (see saver-gate section below).

**Unknown taxes must not be treated as zero.** Some programs (`qatar`, `turkish`,
`singapore`) don't report taxes and return `0`, indistinguishable from a real $0 co-pay. The
parser represents this as `taxes_usd = None`. When taxes are `None`, you cannot compute a
trustworthy effective CPP — **skip with an explicit "can't verify taxes" reason rather than
substituting 0.0**, which would inflate CPP and fire false alerts for exactly those programs.
Make the valuation parameter default to `None`, not `0.0`, so a forgetful caller can't
silently reintroduce the zero-tax bug.

Why effective CPP and not raw miles: 120k miles for a $6,000 business seat (5.0 cpp) is a
screaming deal; 120k miles for a $900 economy seat (0.75 cpp) is a trap. The cash
comparison is what separates them.

**Two-stage cash pricing, mirroring Cached-Search-then-Get-Trips:**
`comparable_cash_price_usd` itself comes from a cheap, cached, ISO-week-bucketed baseline
(see `flight-cash-price-monitor`) — accurate enough to decide who's a *candidate*, but not
accurate enough to be the number that finalizes a real alert. Day-of-week price variance on
long-haul business fares can be large, and whichever exact date happened to trigger that
week's cached lookup becomes the stand-in for every other date in the bucket, which isn't
trustworthy as the final gating number. So: use the cheap/bucketed price for the FIRST-pass
gate (the same role Cached Search plays for award data); once a candidate clears every
other filter (dedup, the per-run alert cap, and the seats.aero Get Trips detail call), spend
**one additional real cash-provider call** for that specific award's EXACT date to confirm
the price before finalizing the verdict and sending — the same role Get Trips plays: a
precision recheck spent only on finalists, not on every candidate, bounded by the same small
numbers (`max_alerts_per_run`). If the exact-date price fails the floor after all, skip with
a distinct, logged reason — never silently fall back to the bucketed estimate as if it were
confirmed.

Per-program floors live in `watchlist.yaml` because "good" differs by currency. Sensible
starting floors (the owner should tune these):

```yaml
cpp_floors:          # cents per point to be worth alerting
  aeroplan:        1.5
  flying_blue:     1.3
  aadvantage:      1.5
  virgin_atlantic: 1.5
  united:          1.3
  default:         1.4
```

Also apply an **absolute-value gate** so we don't alert on a technically-great CPP that
saves $40. Example: only alert if `(comparable_cash_price - award_taxes) >= min_trip_value`
(e.g., $1,500 for the long-haul premium cabins we actually care about).

### 2. Cash price drop

For a revenue fare, alert when the current low drops below its tracked baseline by more than
the configured margin:

```
drop_pct = (baseline_price - current_price) / baseline_price
alert if drop_pct >= min_drop_pct   # e.g., 0.20
   or  (baseline_price - current_price) >= min_drop_abs
```

Never alert before a baseline exists (seed the first observation silently). Mistake fares
show up here as extreme drops — optionally add a separate lower threshold flagged as
"possible mistake fare, book fast."

**Which baseline number is `baseline_price`?** The tracked baseline (see
`flight-cash-price-monitor`) holds two numbers: a trailing minimum and an EMA ("typical"
recent price). Use the **EMA**, not the trailing minimum, as `baseline_price` in the drop
formula — a documented judgment call: a drop *below the all-time trailing low* would almost
never fire, since by definition it's the lowest price ever observed, whereas a drop below
the recent *typical* price is the actual "is this unusually cheap right now" signal a
flash-sale/mistake-fare alert exists to catch. Still surface the trailing minimum in the
alert itself for context (e.g. "$4,200 — previous low was $4,500").

## The saver gate

seats.aero has no per-item saver/standard field to read — dynamic-price filtering is a
**request-time** choice, controlled by the `include_filtered` query param on Cached Search
(see `seats-aero-integration`). Leave it false/omitted so every result the poller sees is
already saver-equivalent; don't build a post-hoc filter looking for a field that isn't
there. If a later version wants to see raw/dynamic pricing too (e.g. to compare), that's a
separate, explicit query with `include_filtered=true`, kept out of the default alert path
so standard/dynamic awards — usually poor value — never silently reach the valuation gate.

## Dedup & debounce (mandatory)

Every alert passes through the state store before sending. The point is to alert on *new*
deals and *meaningful changes*, not the same seat every 20 minutes.

**Dedup key design** — bucket the price so tiny fluctuations don't re-fire:

```python
def award_key(a: AwardAvailability) -> str:
    miles_bucket = a.miles // 5000 * 5000          # 5k-mile buckets
    return f"award:{a.origin}-{a.destination}:{a.date}:{a.cabin}:{a.program}:{miles_bucket}"

def cash_key(f: CashFare) -> str:
    price_bucket = int(f.price_usd // 50 * 50)      # $50 buckets
    return f"cash:{f.origin}-{f.destination}:{f.date}:{f.cabin}:{price_bucket}"
```

**State store contract:**

```python
class StateStore(Protocol):
    def already_alerted(self, key: str) -> bool: ...
    def record_alert(self, key: str, ttl_seconds: int) -> None: ...
    # baselines for cash drop detection:
    def get_baseline(self, route_key: str) -> Baseline | None: ...
    def update_baseline(self, route_key: str, price: float) -> None: ...
```

- Give alert records a **TTL** (e.g., 3–7 days) so a deal that vanishes and genuinely
  returns later can re-alert, but the same standing deal doesn't nag daily. DynamoDB TTL is
  perfect for this.
- Record the alert **only after** a successful send, but guard against double-send on Lambda
  retry (idempotent: check-then-set, or write a "pending" marker). Prefer: send, then record;
  and make the whole poll idempotent so a retry re-evaluates cleanly rather than
  double-firing.
- A *materially better* version of an existing deal (e.g., price crosses into a lower
  bucket) is allowed to re-alert — that's why the bucket is in the key.

**Per-run alert cap, independent of dedup — real incident, not a theoretical concern.**
Dedup (above) stops the *same* deal from re-alerting on a *later* run. It does nothing to
stop a *single* run from alerting on many *different* new deals at once — a wide date
window or a newly widened/added route can surface many qualifying candidates simultaneously
against an empty dedup table. This happened for real: one production invocation alerted 73
times in a single run, from an ~11-month date window against a freshly-created (empty)
dedup table. Add a `max_alerts_per_run` cap (config-as-code in `watchlist.yaml`, e.g. 8),
enforced across the *whole run* (all routes, both award and cash-drop alerts sharing one
budget), not per-route. A candidate that clears every other gate but loses the race for that
run's budget should be logged as "matched but capped" and counted **separately** from
duplicates in the run summary — don't drop it silently, and don't conflate "skipped as
duplicate" with "skipped because the cap was reached," since a future run's logs need to
make it obvious which one was actually the limiting factor.

## Output of this layer

`is_high_value(candidate) -> Verdict` where `Verdict` carries: fire/skip, the reason
(effective CPP or drop %), and a human-readable one-liner the notifier can drop straight
into the message ("Business saver, 4.8¢/pt vs $5,900 cash"). Keep the *why* attached to the
verdict so the alert explains itself.
