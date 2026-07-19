# Session handoff

**Read `CLAUDE.md` first, then the skill under `.claude/skills/` that matches your task.**
This file is just the "where things actually stand right now" snapshot — it will go stale;
the skills and `CLAUDE.md` are the durable source of truth and were updated alongside this
file to reflect everything below.

## Current state

**Deployed to real AWS: still v1.0 + v1.1 + v1.1.1 + v1.2, unchanged since the last session.**
Nothing from this session has been applied to real infrastructure yet — see CLAUDE.md's
"Current deploy status" for the full, current picture. Everything below is local code/config
state: built, tested (261 tests, all green), Terraform plans reviewed where applicable, but
not deployed.

**This session's real, completed work (all local, none applied):**

1. **Heartbeat namespace bug — fixed.** `src/poller.py`'s `HEARTBEAT_NAMESPACE` corrected to
   `"flight-tracker-app/Heartbeat"`; `infra/iam.tf`'s Heartbeat condition reverted to reference
   `local.heartbeat_namespace` directly. A reviewed `terraform plan` shows the expected
   `source_code_hash` change and, notably, zero diff on the IAM condition itself — real AWS
   already had the correct value the whole time (the earlier "stopgap" was only ever an
   uncommitted local file edit, never actually applied — see
   `avoiding-duplicate-implementations`).
2. **Lambda zip build made reproducible** — `scripts/build_lambda_package.sh` now stamps fixed
   mtimes and sorts entries before zipping; verified via three identical rebuilds. See
   `aws-serverless-deploy`.
3. **`lambda_timeout` raised twice: 120s → 300s → 800s.** The 300s figure was based on a real
   65s measurement against an economy-only watchlist; business/first were re-added the SAME
   session (tripling cabin fan-out), and a fresh real measurement immediately after ("Run 1",
   see below) came back at 620.3s total — 800s is the current, up-to-date figure. See
   `aws-serverless-deploy`'s "Lambda timeout" section for the full two-measurement history.
4. **Premium-cabin re-add + free sanity prefilter** (`premium_cabin_max_multiplier`, default
   2.0) — business/first back on both routes alongside economy, with a free (no cash lookup)
   rejection of an obviously-bad premium-cabin candidate. See `deal-valuation`.
5. **`transfer_bonus_pct` annotation** (informational only, never gates anything) —
   `watchlist.yaml`'s `virginatlantic` entry is currently **0.3** (confirmed active
   2026-07-19, expires 2026-07-31, see that file's own comment); every other eligible program
   is still 0.0, unresearched, not confirmed zero. **This is a `watchlist.yaml` change and
   therefore ships in the next Lambda zip rebuild — it is not live in the deployed Lambda
   until that rebuild+deploy happens**, same as any other code/config change.
6. **Group-winner selection** — the largest piece of this session. Per (origin, destination,
   cabin, program, calendar month) group, only the single highest-cpp candidate now reaches
   dedup/cap/Get Trips/exact-confirm/notify; applied identically in the digest's ranking. See
   `deal-valuation`'s full spec, including the real finding that motivated it: Run 1 (below)
   showed the entire `max_alerts_per_run` cap consumed by ONE repeating flat-rate Aeroplan
   award chart across near-duplicate dates. `evaluate_candidate()` was split into
   `classify_candidate()`/`finish_award_candidate()` to make this possible.

**Real cost measurement, "Run 1" (2026-07-19, `scripts/dry_run.py` across both routes,
PRE-grouping code):** `DC → Italy` 155.22s (22 candidates, 1 Cached Search + 0 Get Trips, 0
real SerpApi calls — every candidate was a far-future 2027 date that skipped/timed out before
any cash call completed). `DC → Europe (broad)` 465.08s (4,813 candidates, 8 Cached Search +
12 Get Trips, 159 weekly-baseline + 12 exact-confirm = 171 real SerpApi calls, 8 real alerts
sent — the FULL cap, all 8 the same flat-rate Aeroplan chart — and 91 more genuinely-qualifying
candidates capped afterward). **Combined: 620.3s, 171 SerpApi calls total.** "Run 2" (meant to
run ~20-25 minutes after Run 1, to characterize real steady-state cost with most cash buckets
still warm) was explicitly cancelled mid-session before it started — never executed. Since
this whole measurement predates group-winner selection, treat it as a useful pre-grouping
data point, not the current steady-state baseline — see "Next concrete steps" below.

**BWI's exclusion from `DC → Europe (broad)`'s origins is now a permanent decision, resolved
this session — not an open item.** The original removal (see `watchlist.yaml`'s own comment)
was justified against an economy-only cost projection; business/first have since tripled
per-route cabin fan-out, which only strengthens the case for staying IAD-only rather than
weakening it. `watchlist.yaml`'s comment and `tests/test_config.py`'s real-watchlist
regression test were both updated to reflect this as permanent, not deferred.

**Both EventBridge schedules exist in real AWS and remain confirmed `DISABLED`** —
`award-cached-poll` and `digest-weekly` — unchanged this session, verified via `terraform
state show` in an earlier session. **Neither should be enabled until every item in "Next
concrete steps" below is confirmed clean** — v1.2.1 hasn't even been applied yet.

## Next concrete steps, in order

1. **Confirm/apply the `transfer_bonus_pct.virginatlantic` update.** It's live in
   `watchlist.yaml` (0.3, expires 2026-07-31) but not yet in the deployed Lambda — it needs a
   zip rebuild + real deploy (alongside everything else in this session, since all of it ships
   in the same package) to actually take effect in production, and a reminder to revert it to
   0.0 after 2026-07-31 if the promo isn't renewed.
2. **Re-run Run 1/Run 2 (steady-state cost measurement) now that grouping is built.** The old
   Run 1 numbers (620.3s, 171 SerpApi calls) predate group-winner selection and are expected to
   overstate real cost going forward — Run 1's own log showed the ENTIRE alert cap consumed by
   one repeating flat-rate chart, exactly the case grouping now collapses to one winner before
   Get Trips/exact-confirm are ever spent. A fresh Run 1 (cold-ish) + Run 2 (~20-25 min later,
   most buckets still warm) pair, with grouping active, is what actually answers "what does
   real steady-state polling cost now" — the old numbers shouldn't be trusted for that
   question anymore.
3. **THEN reconsider `max_alerts_per_run` (currently 8) with accurate numbers.** Only after
   step 2's fresh measurement — tuning the cap against pre-grouping numbers (where one program
   could exhaust it alone) would be tuning against a problem grouping already fixes, not the
   real remaining shape of the data.
4. **Real-time schedule (`award-cached-poll`) stays `DISABLED` until 1-3 above are all
   confirmed clean.** This is in addition to, not instead of, the still-pending heartbeat fix
   deploy: even once transfer-bonus/cost/cap are all settled, `terraform apply` +
   `terraform plan` review is still a separate, deliberate step, and enabling either schedule
   is its own later decision after that (see `aws-serverless-deploy`'s two-phase-apply
   discipline).

Everything above is explained in full, with the real supporting data, in `CLAUDE.md`'s "Build
phases" (the new v1.2.1 entry) and "Current deploy status" sections, `deal-valuation`'s
group-winner-selection section, and `aws-serverless-deploy`'s "Lambda timeout" section.
