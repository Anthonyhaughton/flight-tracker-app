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
  about — this was originally long-haul business/first, but as of the economy-cabin
  recalibration (see below) both active routes now watch **economy**, so "cares about" is a
  `watchlist.yaml` config choice per route, not a fixed cabin tier. Saver-equivalence is
  handled at request time, not here (see saver-gate section below).

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

Per-program floors live in `watchlist.yaml` because "good" differs by currency, AND — see
"Real validated calibration data" below — because the right number differs enormously by
*cabin*, not just program. **Never treat a floor as correct just because it's committed to
config; validate it against real data before trusting it** (this is exactly what the
economy-cabin recalibration below had to do, because the original floors were an untested
guess inherited from a different cabin tier).

Also apply an **absolute-value gate** so we don't alert on a technically-great CPP that
saves $40: only alert if `(comparable_cash_price - award_taxes) >= min_trip_value`. Same
validate-before-trusting caveat applies — see below.

## Program eligibility (`eligible_programs`) — reward-transfer-partner filtering

A third gate, applied earliest (before the cabin/cash logic above ever runs): a candidate's
`program` (seats.aero's source key) must be in `watchlist.yaml`'s top-level
`eligible_programs` list, or it's rejected before a cash lookup or Get Trips call is ever
spent on it. This exists because the owner can only actually book a redemption through a
program they can get miles into — award space in a program with no realistic path to earn
or transfer into it is not a real deal, however good the CPP math looks.

**Design: the union of Amex Membership Rewards' and Chase Ultimate Rewards' airline transfer
partners, cross-referenced against real (not documented — see `seats-aero-integration`'s
finding on the Concepts doc's Sources table being wrong) seats.aero source keys.** A program
not reachable via either card's transfer partnerships has no legitimate path onto this list,
regardless of how attractive its award space looks.

**Explicit philosophy: do not prune this list based on assumptions about which airlines fly
where.** Award availability reflects a program's *alliance and partner network*, not just
that airline's own routes — a redemption can be real, bookable space with zero involvement
from the airline whose loyalty program it's priced in. The concrete example that motivated
this rule: `aeromexico` (a SkyTeam program) can surface genuine European award availability
with no AeroMexico metal anywhere in the itinerary, purely through SkyTeam partner access.
Pruning `eligible_programs` down to "airlines that plausibly fly this route" would have
silently cut a real category of sweet spots. The only valid basis for removing a program is
**real, observed, sustained zero-hits evidence** — not a guess made today. `poll_route()`
logs every program that actually appears in real Cached Search results each run
(deliberately unfiltered by `eligible_programs`, so it also surfaces programs outside the
list entirely), specifically so a pruning decision can be made from a couple of weeks of real
logs, not intuition.

## Real validated calibration data (as of the economy-cabin recalibration)

The floors above are not abstract defaults — they were tuned against real production data
after both active routes switched from business/first to **economy**. The original
1.3–1.5cpp / $1,500 pair had been implicitly tuned for business/first and, left in place for
economy, would have rejected nearly every real candidate on trip value alone before CPP ever
mattered (median real economy trip value came in around $293–306 — comfortably above a
$250 floor, well below the old $400/$1,500-era numbers).

**Validated via `scripts/dry_run.py`'s `--cpp-floor`/`--min-trip-value` overrides against
thousands of real IAD → Europe economy candidates:**

- **`cpp_floor`: 2.0** (down from 2.5, itself down from the original 1.3–1.5 business/first
  numbers). Real median CPP across the observed data: **~0.83cpp** — the vast majority of
  real economy inventory clears neither 2.0 nor the old 2.5, so lowering the floor did not
  open the gate to "anything fires." Real observed ceiling: **~2.82cpp**, consistently, across
  multiple runs and multiple destinations (LHR, CDG, AMS) — and every single real award alert
  sent during this validation was the exact same find: **Virgin Atlantic, 12,000 miles**.
  That's the dominant/only real signal seen so far, not a representative sample of "many good
  deals" — treat 2.0 as validated against real data, not as proof the gate finds broad
  variety yet. More runs over more time are what would tell the difference.
- **`min_trip_value_usd`: $250** (down from $400). This was the more consequential of the two
  changes in practice — the median real candidate's trip value sits above $250 but below the
  old $400, so this floor was doing more of the real gatekeeping work than the CPP change.

**The Qantas finding — a structurally bad redemption, not a bug.** IAD → LHR economy on
`qantas`, weeks 2026-W37–W39: real cash comparison $299, real carrier-imposed taxes $349 (from
seats.aero's own `TotalTaxes` field) — trip value is **−$50**, CPP slightly negative. This is
not a mismatched comparison (both numbers are genuinely sourced for the same route/cabin/
week) and not a today-only fluke — it's a **permanent characteristic of that program on that
route**: the real carrier surcharges structurally exceed the cash price, so redeeming there
would cost more in taxes than buying the ticket outright, on top of burning miles. The gate
correctly rejects this via the trip-value floor. Worth knowing this exists as a category —
some program/route combinations are just bad, consistently, and won't become good deals no
matter how the CPP floor is tuned.

## The fallback-direction fix: no resolved cash price always skips

**A real safety issue found in an architecture review, fixed, not hypothetical.** An earlier
version had a per-route `require_cash_comparison` opt-out: when `False` (the default), a
candidate with no resolved cash price (provider error, or a route with genuinely no cash data
yet) fell back to firing on cabin-match alone — "v0-style" behavior, justified at the time as
"an award pipeline shouldn't be blocked by a cash-side outage."

**That fallback direction was backwards for a system whose top priority is avoiding alert
fatigue.** It meant the cash pipeline's failure mode was *more* alerts, not fewer — exactly
the wrong direction when the provider degrades. `require_cash_comparison` was **removed
entirely** (not just defaulted off) — there is no route-level opt-out anymore. No resolved
cash price now unconditionally skips, with a clear logged reason
(`"cash data unavailable, skipping rather than firing blind"`), on every route, no exceptions,
regardless of whether the "no data" came from a provider exception or a genuine empty result.
If a future feature needs a cash-optional path again, it must not reintroduce a silent,
more-permissive default — any such path should require an explicit, deliberate opt-in with
its own justification, not inherit this as the default behavior.

## The weekly digest (built, tested, not yet live-verified)

The real-time triggers above (award CPP gate, cash-drop, mistake-fare ceiling) are
deliberately conservative — see the honest architecture-review finding that **silence is the
normal outcome and is indistinguishable from failure**: a route with nothing above the real
bar produces zero output, identical to a broken pipeline. The digest is the fix, and is
**additive, not a replacement** — the real-time triggers keep working exactly as they do now.

Implemented in `src/digest.py`, wired into `src/poller.py`'s `run_digest()` and
`lambda_handler` (dispatches on a distinct `{"mode": "digest"}` event payload — every other
event preserves the real-time path exactly, see
`test_lambda_handler_default_path_is_byte_for_byte_unchanged`) and into
`scripts/dry_run.py --mode digest` for local live testing. 18 tests, all mocked; not yet run
against real live data (see `SESSION_HANDOFF.md` for the next concrete step).

**Snapshot-at-digest-time, not week-long accumulation:** each run is a fresh Cached Search
across every active route at digest-build time, ranked and sent as one aggregate message —
not an accumulation of candidates seen incrementally over the preceding week.

- **Cadence:** weekly, via a **second EventBridge schedule** invoking the same Lambda with a
  distinct event payload (`{"mode": "digest"}` vs the existing cached-poll invocation) — no
  new Lambda, no new deployment artifact. (The EventBridge schedule itself hasn't been created
  yet, even as an unreviewed Terraform plan — see `CLAUDE.md`'s v1.2 status.)
- **Content:** `build_weekly_digest()` walks every active route: real Cached Search →
  `eligible_programs`/cabin prefilter (`passes_award_prefilter`, same gate the real-time path
  applies, so an ineligible program never costs a cash lookup here either) → for every
  surviving candidate — not just gate-passers — CPP and trip value are computed
  (`compute_effective_cpp`, the exact same function `evaluate_candidate()` uses) off the
  cheap, already-cached weekly-bucketed baseline (`get_or_refresh_baseline`). A candidate with
  unknown taxes (`taxes_usd is None`) is excluded from ranking, same rule as
  `is_high_value`'s zero-inflation guard above. Two independent top-5 rankings result: top 5
  by cash trip value, top 5 by CPP — ten total only when the lists are fully disjoint; a
  candidate can and does appear in both.
- **Cost-bounded ranking, mirroring the two-stage cash-pricing pattern already in place:**
  ranking itself costs nothing beyond the SAME bounded, cache-first baseline lookup the
  real-time path already pays for. Then exactly **one real exact-date-confirm call
  (`confirm_exact_date_price`) per DISTINCT finalist** — the union of both top-5 lists,
  deduped by `availability_id` — not per-list and not per-candidate-seen: a candidate present
  in both the cash-value and CPP top-5 costs one call, not two, so the real bound is "at most
  10, often fewer," never "up to 10 each." No Get Trips call is spent at all — the digest
  reports Cached Search's own taxes, only re-pricing the cash side (the one number day-of-week
  variance actually makes unreliable at the weekly-bucket level).
- **Always sends, even an honestly empty digest:** when nothing survives ranking, both lists
  are empty and the notifier sends "no award availability found this week" rather than
  nothing at all. When candidates exist but none of the confirmed finalists would have
  cleared the real-time bar, the message names the closest miss — "nothing cleared the
  real-time bar this week, closest was X.Xcpp on program Y (floor Z.Zcpp)" — computed by
  running `is_high_value` (the exact real-time gate function, not a re-derived condition)
  against each finalist's CONFIRMED numbers, so "would this have fired a real alert" is
  authoritative, not approximated.
- **Its own orchestration, deliberately NOT `evaluate_candidate()`:** a "shared lower layer,
  different upper orchestration" case, per `avoiding-duplicate-implementations` — dedup, the
  per-run `max_alerts_per_run` cap, and single-alert-notify all describe a *gate-then-maybe-
  send-one-alert* shape that doesn't fit a digest that ranks EVERY candidate and sends exactly
  one aggregate message with no dedup and no cap. Forcing `evaluate_candidate()` to serve both
  shapes would mean either silently dropping near-misses the digest exists to surface, or a
  special-cased `evaluate_candidate()` that knows about digest mode — a different kind of
  duplication. What IS shared: `passes_award_prefilter`, `compute_effective_cpp`,
  `is_high_value`, `get_or_refresh_baseline`, `confirm_exact_date_price` — the actual valuation
  math, imported and called directly, never re-derived.
- **Notifier delivery:** `Notifier.send_digest()` on both `DiscordNotifier` (two embeds — Top
  Cash Value, Top CPP — one message's embed set, not ten separate alert-style messages) and
  `TelegramNotifier` (one MarkdownV2 message, two sections), kept in sync per this project's
  standing Discord-default/Telegram-swappable practice.

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
