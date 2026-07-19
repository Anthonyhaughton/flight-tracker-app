# Session handoff

**Read `CLAUDE.md` first, then the skill under `.claude/skills/` that matches your task.**
This file is just the "where things actually stand right now" snapshot — it will go stale;
the skills and `CLAUDE.md` are the durable source of truth and were updated alongside this
file to reflect everything below.

## Current state

**Deployed to production: v1.0 only — award-only, business/first cabin, no cash
integration, the original thresholds, no `eligible_programs` filter.** The EventBridge
schedule has **never been enabled** at any point in this project's history — the Lambda has
never fired on its own, only via manual invokes during v1.0 verification.

**Built, tested, and locally live-verified — but never deployed:** everything from v1.1
through v1.1.1. That includes real cash integration (SerpApi, baselines, exact-date confirm),
the `eligible_programs` reward-transfer-partner filter, the pivot of both active routes to
economy cabin, a real-data-recalibrated `cpp_floor`/`min_trip_value_usd` (2.0/$250, down
from thresholds implicitly tuned for a different cabin), the removal of a real
alert-fatigue safety issue (a permissive cash-outage fallback that used to make a
cash-provider outage produce *more* alerts, not fewer), and a refactor that unified
`poll_route()`'s and `scripts/dry_run.py`'s per-candidate logic into one shared
`evaluate_candidate()` function (with an object-identity test preventing future drift — see
the `avoiding-duplicate-implementations` skill). A `terraform plan` for all of this has been
reviewed and is clean (source-code-hash-only diff) but **has not been applied**.

**Built and tested — but NOT yet live-verified, and never deployed: v1.2, the weekly
digest.** `src/digest.py`'s `build_weekly_digest()` implements the full confirmed spec —
snapshot-at-digest-time ranking across every active route, two independent top-5 rankings
(cash-value, CPP) off the cheap weekly-bucketed cash estimate, exactly one real exact-date
confirm call per DISTINCT finalist (deduped across both lists, never per-list), and an
always-sends guarantee (an honestly empty digest, or a "closest miss" message, when nothing
clears the real-time bar). It deliberately does NOT call `evaluate_candidate()` — a "shared
lower layer, different upper orchestration" case (see `avoiding-duplicate-implementations`):
the shared valuation math (`passes_award_prefilter`, `compute_effective_cpp`, `is_high_value`,
`get_or_refresh_baseline`, `confirm_exact_date_price`) is imported and called directly, not
re-derived. Wired into `lambda_handler` via `{"mode": "digest"}` (every other event is
provably unchanged — see `test_lambda_handler_default_path_is_byte_for_byte_unchanged`) and
into `scripts/dry_run.py --mode digest`. `Notifier.send_digest()` implemented on both
`DiscordNotifier` (two embeds: Top Cash Value, Top CPP) and `TelegramNotifier` (one
MarkdownV2 message, two sections). 18 new tests, all mocked; full suite (208 tests) green.
**This is a step earlier than v1.1/v1.1.1 above**: `--mode digest` has only been
smoke-tested with a fake key (confirmed it reaches a real seats.aero call and fails loudly on
auth — proof the wiring is live, not proof the ranking output is sane against real data). See
"Next concrete task" below.

**One more real gap inside the current config itself:** the real `watchlist.yaml`'s
`DC → Europe (broad)` route sets `origins: [IAD, BWI]`, but only `IAD` has ever actually been
queried by anything — real API calls or local dry runs. `BWI` would go live untested the
moment this deploys.

Everything above is explained in full, with the real supporting data, in:
- `CLAUDE.md`'s "Build phases" and "Current deploy status" sections (the authoritative
  status summary).
- `deal-valuation` skill: the `eligible_programs` design philosophy, the real validated
  economy-cabin calibration data (median CPP, the Virgin Atlantic finding, and the Qantas
  structurally-negative-value finding), the fallback-direction fix, and the weekly digest's
  full implementation (ranking, dedup, always-sends guarantee).
- `seats-aero-integration` skill: the finding that the live Concepts doc's Sources table is
  not authoritative (British Airways / Iberia were absent from the doc but real in live
  results).
- `aws-serverless-deploy` skill: three real operational lessons from live testing (local
  dry-run state vs. production DynamoDB state, verifying actual state after an interrupted
  tool call, verifying real cost at the provider's dashboard rather than trusting a running
  tally).
- `avoiding-duplicate-implementations` skill: the general pattern behind the
  `evaluate_candidate()` refactor, AND (its second real case) why the digest needed its own
  orchestration instead of forcing `evaluate_candidate()` to serve a shape it wasn't built for.

## Next concrete task: one real live-data run of the digest

This is next because everything else about v1.2 is done except the one thing that can't be
verified with mocks: whether the ranking output looks sane against real seats.aero/SerpApi
data, and whether the real call volume matches what was designed (bounded, cache-first
weekly-baseline lookups plus at most 10 real exact-date confirms). This mirrors exactly how
v1.1 was live-verified via `scripts/dry_run.py` before v1.1.1 was ever built on top of it —
same discipline, same script, new `--mode`.

**Run:**

```
python scripts/dry_run.py --mode digest
```

**What a successful run should confirm** (see `scripts/dry_run.py`'s own pre-flight log line
for the exact call-count bound before it runs):

- Real Cached Search hits across every active route, at the exact call count the pre-flight
  estimate predicted (deterministic: origins × destinations, summed across active routes).
- The weekly-baseline SerpApi call count is small and cache-first — most candidates sharing a
  route/cabin/ISO-week bucket should NOT each cost a separate call.
- At most 10 real exact-date-confirm calls, and fewer than 10 if the two top-5 lists overlap
  (check the log — this is the thing a mocked test can assert but can't prove against real
  data volume/shape).
- A real Discord message lands with two sections (Top Cash Value, Top CPP) and sane-looking
  numbers — or, if nothing clears the real-time bar this week, an honest "closest was
  X.Xcpp" message, not silence and not a crash.
- If the real run instead turns up an honestly-empty digest (no candidates ranked at all),
  that's a valid, useful outcome too — it's a signal about current real availability, not a
  bug, the same way a quiet real-time run is normal on most weeks (see `deal-valuation`).

**After this run succeeds:** v1.2 moves to the same status tier v1.1.1 already sits at
(built, tested, locally live-verified, never deployed) — update `CLAUDE.md`'s v1.2 bullet
accordingly. Deploying is a separate, later decision — see below.

## The other pending decision: closing the deploy gap

Independent of the digest work above — whenever it's time to actually ship v1.1/v1.1.1 to
production, mirror the v1.0 two-phase pattern (`aws-serverless-deploy`):

1. `terraform apply` the already-reviewed plan (Lambda `source_code_hash` update only,
   `schedule_enabled` stays `false`).
2. Verify with a real manual Lambda invoke — confirm the new code path (cash gating,
   `eligible_programs`, economy cabin, the corrected fallback behavior) behaves correctly
   against real production DynamoDB state, not just `scripts/dry_run.py`'s local JSON state
   (these are genuinely separate stores — see `aws-serverless-deploy`'s live-testing
   lessons).
3. Only then apply again with `schedule_enabled = true`.

Before step 2 specifically: assume a **fully cold cache** for the real DynamoDB baselines
table when estimating that first invoke's API call cost — local dry-run testing, however
extensive, has never touched the real DynamoDB tables and will not have warmed them at all.

**v1.2's deploy gap is a step further back than v1.1/v1.1.1's:** there is no reviewed
Terraform plan for the digest at all yet — the second EventBridge schedule (weekly cadence,
`{"mode": "digest"}` payload, same Lambda) hasn't been drafted, let alone reviewed or applied.
That work hasn't started and shouldn't, until the live-verification run above has actually
happened — no reason to write infra for a ranking path that hasn't been checked against real
data yet.

This can happen before or after the digest's live-verification run above — they're
independent decisions. If v1.1/v1.1.1 ships to production first, the digest work sits as
*more* untested-in-production code on top of an already-undeployed-then-deployed base, which
is fine, but worth being deliberate about rather than accidental.
