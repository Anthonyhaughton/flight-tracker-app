# CLAUDE.md — Flight & Award Deal Agent

This file is the operating brief for any agent working in this repo. Read it fully
before writing code. When a task maps to one of the skills in `.claude/skills/`,
consult that skill first — it holds the detailed, current guidance.

## Mission

Monitor **cash flight prices** and **award (points/miles) availability** out of the
Washington D.C. airports (IAD, DCA, BWI) toward Europe (Italy first), and push a
**Discord alert (default notifier; Telegram is a swappable alternate) only when a
genuinely high-value deal appears.** High-value is a
computed judgment (see `deal-valuation`), not "any seat exists" — the fastest way to
kill this project is to spam the owner into muting the bot.

This is a **personal, non-commercial** tool. That constraint is not decoration: it is a
hard requirement of the seats.aero API terms and it keeps us on the right side of the
data providers. Do not build anything that resells data, runs for third parties, or
scrapes airline/OTA sites directly to dodge those terms.

## Core architecture (two data planes)

```
                 ┌─────────────────────┐
  EventBridge ─▶ │  poller (Lambda)     │
  (schedule)     │                      │
                 │  ┌────────────────┐  │   award space
                 │  │ seats-aero     │──┼──▶ (cached search → get trips detail)
                 │  └────────────────┘  │
                 │  ┌────────────────┐  │   cash fares
                 │  │ cash provider  │──┼──▶ (SerpApi wrapper, swappable)
                 │  └────────────────┘  │
                 │  ┌────────────────┐  │
                 │  │ valuation gate │  │   CPP + thresholds → is this "high value"?
                 │  └────────────────┘  │
                 │  ┌────────────────┐  │
                 │  │ dedup (Dynamo) │  │   already alerted? skip.
                 │  └────────────────┘  │
                 │  ┌────────────────┐  │
                 │  │ notifier send  │──┼──▶ Discord webhook (default) / Telegram Bot API
                 │  └────────────────┘  │
                 └─────────────────────┘
```

The single most important design principle: **do not scrape where a structured source
exists.** Award space comes from seats.aero (which already normalizes across all three
alliances). Cash fares come from a paid scraping-API wrapper that returns JSON. We never
run our own stealth browser against Google or airline sites unless a skill explicitly
says a narrow gap requires it.

## Default stack (and the swap points)

These are defaults chosen for a low-frequency, bursty, cheap-to-run workload. Each
"swap point" is isolated behind an interface so it can be changed without touching
business logic. Do not hardwire a provider into the valuation or alert code.

| Concern            | Default                         | Swap point |
|--------------------|---------------------------------|------------|
| Language           | Python 3.12                     | —          |
| Award data         | seats.aero Pro Partner API      | none realistic; it's the best source |
| Cash fare data     | SerpApi Google Flights          | `CashFareProvider` interface (Scrapfly, Zyte, Duffel) |
| Compute            | AWS Lambda (container or zip)    | `deploy/` module (Fargate task, Fly.io, VPS + cron) |
| Schedule           | EventBridge Scheduler           | any cron trigger |
| State              | DynamoDB (dedup + baselines)     | `StateStore` interface (SQLite/Redis) |
| Secrets            | AWS SSM Parameter Store / Secrets Manager | `secrets.py` loader |
| IaC                | Terraform                       | `infra/` (CDK/SAM alternative) |
| Notifications      | Discord webhook (default)       | `Notifier` interface (Telegram is the built-in swappable alternate) |

If the owner wants GCP or a plain VPS instead of AWS, only the `infra/` and `deploy/`
layers change. The poller, providers, valuation, and notifier are cloud-agnostic
Python.

## Repo layout (target)

```
flight-deal-agent/
├── CLAUDE.md
├── watchlist.yaml            # config-as-code: routes, cabins, date windows, thresholds
├── src/
│   ├── poller.py             # Lambda entrypoint; orchestrates the flow
│   ├── providers/
│   │   ├── seats_aero.py     # award space
│   │   └── cash/
│   │       ├── base.py       # CashFareProvider interface
│   │       └── serpapi.py    # default impl
│   ├── valuation.py          # CPP + thresholds → "high value?" decision (award + cash-drop)
│   ├── cash.py                # cash baseline caching/refresh + exact-date confirm step
│   ├── state.py              # StateStore interface + DynamoDB impl (dedup, baselines)
│   ├── notify/
│   │   ├── base.py           # Notifier interface
│   │   ├── discord.py        # default impl (webhook, rich embeds)
│   │   └── telegram.py       # swappable alternate impl (bot token, MarkdownV2)
│   ├── config.py             # loads watchlist.yaml + CPP valuations
│   └── secrets.py            # pulls API keys from SSM/Secrets Manager
├── infra/                    # Terraform
├── tests/
└── .claude/skills/           # the skills below
```

## Non-negotiable conventions

- **Secrets never touch git.** `SEATS_AERO_API_KEY`, `DISCORD_WEBHOOK_URL`, `SERPAPI_KEY`,
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` load at runtime from SSM/Secrets Manager (locally:
  `.env`, which is gitignored). No key literals in code, tests, or Terraform state. Verify
  `.gitignore` covers `.env` and `*.tfvars` before the first commit. Local `.env` and the
  deployed SSM values are two separate stores that do **not** auto-sync — see
  `secrets-hygiene` (this caused a real production auth failure once).
- **Config as code.** Routes, cabins, date windows, per-program CPP valuations, and alert
  thresholds live in `watchlist.yaml`, not in code. Adding a route is a config edit, not
  a deploy of new logic.
- **Dedup is mandatory.** Every alert path goes through the state store. Never send a
  notifier message (Discord or Telegram) without first checking + recording a dedup key.
  See `deal-valuation` for the key design, including the per-run `max_alerts_per_run` cap
  (independent of dedup) that guards against many *new* deals firing in a single run.
- **Respect rate limits.** seats.aero Pro access is a 1,000-calls/day quota that resets at
  00:00 UTC (not a per-minute limit). Poll *Cached Search* frequently and cheaply; spend a
  *Get Trips* call only on a candidate that already cleared the valuation gate, right before
  alerting, for fresher per-trip detail. Live Search is commercial-partner-only and not
  available on a Pro account — don't design around it. On 429 (quota exhausted), stop for
  the run; there's no reset until midnight UTC regardless of backoff.
- **Idempotent pollers.** A poll run must be safe to retry. No partial-send states that
  double-alert on Lambda retry.
- **Fail loud, not silent.** A poller that dies quietly is worse than useless because
  "no alerts" then means nothing. Wire a heartbeat / dead-man's-switch (see
  `aws-serverless-deploy`).
- **Least privilege.** The Lambda role gets exactly the DynamoDB tables and secret ARNs it
  needs, nothing wildcard.
- **Manual invokes always override the CLI's read timeout.** Any manual `aws lambda invoke`
  against the poller must pass `--cli-read-timeout` set comfortably above the Lambda's
  currently configured `lambda_timeout` (300s as of 2026-07-19 — see `infra/variables.tf`).
  The AWS CLI's own default read timeout (60s) is shorter than realistic real execution
  time; without the override, a slow invoke makes the CLI silently fire a duplicate real
  invocation via its own retry logic — each one a real, budget-spending run, invisible to
  whoever ran the command. This is exactly what happened in production once — see
  `aws-serverless-deploy`'s standing rule.

## What counts as "high value"

Defined in full in the `deal-valuation` skill. In one line: an award is worth alerting on
when it is **saver-priced**, in a **cabin the owner cares about** (a `watchlist.yaml`
per-route config choice — both active routes watch **economy AND business/first** as of
2026-07 (business/first re-added alongside economy, not in place of it — see
`deal-valuation`'s real validated economy-cabin calibration and its premium-cabin sanity
prefilter section for how a bad premium-cabin redemption is rejected for free before it ever
costs a cash lookup), its **program is one the owner can actually book through** (Amex MR /
Chase UR transfer partners — `eligible_programs`, see `deal-valuation`), and its **effective
cents-per-point beats the owner's floor for that program** — or when a cash fare drops
meaningfully below
its tracked baseline. Availability alone is never the trigger. The cash price behind that
CPP math is itself two-stage (a cheap, cached weekly estimate decides candidacy; one precise
real call confirms the exact date before a real alert sends) — see `deal-valuation`. A
candidate with no resolved cash price always skips — never falls back to firing on cabin
match alone, on any route, no exceptions (a real safety fix — see `deal-valuation`).

## Build phases (ship v1 before touching the hard part)

Prove the pipeline end-to-end on the *clean* data source first, then add the part where
all the anti-bot pain lives.

- **v1.0 — award-only. ✅ DEPLOYED and confirmed working end-to-end in production.**
  seats.aero cached search → cabin filter (business/first) → valuation gate → dedup →
  Discord (default notifier; Telegram is a swappable alternate impl, see
  `telegram-alerting`). Verified via a real manual Lambda invoke: real seats.aero data →
  real valuation → a real Discord alert delivered. Deployed via Terraform using a two-phase
  apply (schedule created disabled, verified with one manual invoke, then enabled — see
  `aws-serverless-deploy`). **No longer the only code deployed — see "Current deploy
  status" below for what's actually live now.**
- **v1.1 — cash + real valuation. ✅ BUILT, TESTED, LOCALLY LIVE-VERIFIED, AND NOW
  DEPLOYED** (2026-07-19, code-live in the Lambda — see "Current deploy status" below).
  `CashFareProvider` (SerpApi) implemented against the live API reference; cash
  baselines (trailing-min + EMA, ISO-week-bucketed to bound provider call volume) with a real
  exact-date confirm step for finalists before a real alert sends; real effective-CPP gating
  (`comparable_cash_usd` is no longer always `None`); a second, independent cash-price-drop
  trigger. 150+ tests pass, all mocked, zero real network. Live-verified via
  `scripts/dry_run.py` (NOT the deployed Lambda — dry_run.py uses a local JSON state file,
  completely separate from the real DynamoDB tables, see `aws-serverless-deploy`'s
  live-testing lessons) in both directions: a real run sent a real Discord award alert with a
  real confirmed price/CPP in the footer (not the v1.0 "no cash comparison yet" placeholder);
  a follow-up run with `cpp_floors` deliberately inflated to an unreachable value confirmed
  the real CPP gate correctly rejects. Dedup confirmed to record state only on an actual
  send, never on a valuation-rejected candidate. **"Production-verified" in earlier revisions
  of this file meant "verified locally against live APIs," not "deployed" — that wording was
  corrected here because it's easy to misread as a deploy claim.**
- **v1.1.1 — reward-transfer filtering, economy pivot, real threshold calibration,
  fallback-direction fix, shared-logic refactor. ✅ BUILT, TESTED, LOCALLY LIVE-VERIFIED,
  AND NOW DEPLOYED** (2026-07-19 — see "Current deploy status" below). Everything in this
  phase is real, working, and code-live in the Lambda as of this deploy:
  - `eligible_programs` allow-list (Amex MR / Chase UR transfer partners, cross-referenced
    against live — not documented, see `seats-aero-integration` — seats.aero source keys).
  - Both active routes switched from business/first to **economy**.
  - `cpp_floor`/`min_trip_value_usd` recalibrated to 2.0/$250 against real economy data (see
    `deal-valuation` for the full validated numbers, the Virgin Atlantic finding, and the
    Qantas structurally-negative-value finding).
  - The cash-outage fallback direction was fixed: `require_cash_comparison` (a per-route
    opt-out) was **removed entirely** — no resolved cash price now unconditionally skips, on
    every route, no exceptions (see `deal-valuation`).
  - `poll_route()`'s and `scripts/dry_run.py`'s per-candidate logic was unified into one
    shared `evaluate_candidate()` function, with an object-identity test preventing future
    drift (see the new `avoiding-duplicate-implementations` skill).
  - A `terraform plan` was reviewed (source-code-hash-only diff, confirmed clean) and has
    since been **applied** (2026-07-19 — see "Current deploy status").
- **v1.2 — the weekly digest. ✅ BUILT, TESTED, LOCALLY LIVE-VERIFIED, AND NOW DEPLOYED**
  (2026-07-19 — code-live; the `digest-weekly` EventBridge schedule exists in real AWS but
  is `DISABLED`, same two-phase discipline as the original schedule; see "Current deploy
  status" below). `src/digest.py`'s
  `build_weekly_digest()`: for every active route, real Cached Search → `eligible_programs`/
  cabin prefilter → CPP/trip-value computed for every prefilter-passing candidate (not just
  gate-passers) off the cheap weekly-bucketed cash estimate → two independent top-5 rankings
  (cash-value, CPP) → exactly one real exact-date cash-confirm call per DISTINCT finalist
  across the union of both lists (deduped, never per-list) → always produces a digest, even
  an honestly empty one. Deliberately its OWN orchestration, not a call into
  `evaluate_candidate()` — a "shared lower layer, different upper orchestration" case (see
  `avoiding-duplicate-implementations`): dedup/the per-run cap/single-alert-notify don't apply
  to a digest that ranks everything and sends one aggregate message. Wired into
  `lambda_handler` via a distinct `{"mode": "digest"}` event payload (every other event is
  provably behavior-preserving — see `test_lambda_handler_default_path_is_byte_for_byte_unchanged`)
  and into `scripts/dry_run.py` via `--mode digest`, which also now honors `--origins`/
  `--destinations` (applied uniformly across every active route, since digest mode has no
  single `--route` to scope to). 18 new tests, all mocked, full suite green. **Live-verified
  2026-07-19** via a real `scripts/dry_run.py --mode digest --origins IAD --destinations
  LHR,BCN` run: 960 real candidates evaluated, 516 ranked, both top-5 lists came back full,
  a real Discord message sent (two embeds, not confirmed byte-for-byte but the send
  succeeded cleanly), ~32–34 real SerpApi calls total (24 confirmed exactly via
  `scripts/.dry_run_state.json` timestamp diffing, the rest a bounded estimate) — well
  inside the pre-flight estimate. The Italy-redirected slice of that same run (destinations
  overridden onto a route that doesn't normally use them) cost zero extra calls and produced
  zero ranked candidates, confirmed via the state file showing no new baseline buckets.
  Terraform work for this phase is also done: the second EventBridge schedule
  (`digest-weekly`, `{"mode": "digest"}` payload, `DISABLED` by default) exists in `infra/`
  and has been applied for real (see "Current deploy status" below) — the existing scheduler
  IAM role/policy required no changes, verified by reading, not assumed.
- **v1.2.1 — heartbeat fix, Lambda-timeout recalibration, premium-cabin re-add, transfer-bonus
  annotation, group-winner selection. ✅ BUILT AND TESTED LOCALLY; ⏳ NOT YET APPLIED TO REAL
  AWS.** All of the following are real, working, code-complete, and covered by the full 261-test
  suite (green) — but this entire phase is still local Terraform/code state, not deployed:
  - **Heartbeat namespace bug, fixed.** `src/poller.py`'s `HEARTBEAT_NAMESPACE` corrected from
    the stale `"flight-deal-agent/Heartbeat"` to `"flight-tracker-app/Heartbeat"`;
    `infra/iam.tf`'s Heartbeat condition reverted to reference `local.heartbeat_namespace`
    directly (removing the interim stopgap literal a prior session added). A reviewed
    `terraform plan` shows the expected single `source_code_hash` change and, notably, **zero
    diff on the IAM condition itself** — real AWS already had the correct value the whole
    time, since the "stopgap" turned out to have only ever existed as an uncommitted local
    file edit, never actually applied (see `avoiding-duplicate-implementations`). Not yet
    applied.
  - **Lambda zip build made reproducible.** `scripts/build_lambda_package.sh` now stamps every
    file to a fixed mtime and feeds `zip` a sorted entry order before packaging — verified via
    three consecutive rebuilds producing the identical `source_code_hash` (two of them
    confirmed byte-for-byte via `cmp`). See `aws-serverless-deploy`.
  - **`lambda_timeout` raised twice, 120s → 300s → 800s** — see `aws-serverless-deploy`'s
    "Lambda timeout" section for the full two-measurement history. The current 800s figure is
    based on a REAL 620.3s full-cycle measurement across both routes at the current 3-cabin
    (economy + business + first) scope. Not yet applied.
  - **Premium-cabin re-add + free sanity prefilter.** Business/first re-added to both active
    routes alongside economy (not in place of it); a new `premium_cabin_max_multiplier`
    (default 2.0) rejects an obviously-bad premium-cabin candidate — miles cost > 2x economy's
    on the SAME seats.aero record — before any cash lookup, at zero extra API cost (see
    `deal-valuation`).
  - **`transfer_bonus_pct` annotation, informational only.** Per-program manually-maintained
    transfer-bonus fraction; when nonzero, both notifiers show an effective-points-cost
    annotation alongside the real miles number, never affecting any gate. `virginatlantic` is
    currently set to **0.3** (confirmed active 2026-07-19, expires 2026-07-31 — see
    `watchlist.yaml`'s own comment); every other eligible program is still 0.0, unresearched.
    This is a `watchlist.yaml` change — like any code/config change, it ships in the next
    Lambda zip rebuild+deploy, not automatically.
  - **Group-winner selection**, the biggest piece of this phase: per (origin, destination,
    cabin, program, calendar month) group, only the single highest-cpp candidate now reaches
    dedup/cap/Get Trips/exact-confirm/notify — see `deal-valuation`'s full spec, including the
    real finding that motivated it (one flat-rate Aeroplan chart alone had consumed the ENTIRE
    `max_alerts_per_run` cap across near-duplicate dates in a real Run 1 measurement, see
    below). Applied identically in `src/digest.py`'s ranking. `src/poller.py`'s
    `evaluate_candidate()` was split into `classify_candidate()`/`finish_award_candidate()` to
    make this possible; the shared-function identity test now covers both halves.
- **v1.3 — further controls.** Inline-keyboard mute/snooze, heartbeat alarm tuning.
- **v2 — breadth.** "Anywhere in Europe" inspiration search, more programs, mistake-fare
  detection.

## Current deploy status (read this before assuming anything is live)

**The Lambda's real DEPLOYED code is still v1.0 + v1.1 + v1.1.1 + v1.2, applied directly from
a reviewed Terraform plan on 2026-07-19** — confirmed via a real `terraform plan` refresh
against the live backend (not assumed): the deployed `source_code_hash` matches the build that
includes cash integration, `eligible_programs`, the economy pivot, the recalibrated
2.0cpp/$250 thresholds, the corrected cash-outage fallback, and `src/digest.py`'s weekly
digest. **Everything in "v1.2.1" above (heartbeat fix, timeout raise, premium-cabin/transfer-
bonus/group-winner-selection) is local-only as of this writing — built, tested, terraform
plans reviewed where applicable, but NOT applied/deployed.** Always verify real Terraform/AWS
state before assuming what's live; don't trust a prior session's "not yet applied" note as
still current, in either direction.

**Both EventBridge schedules exist in real AWS and are both confirmed `DISABLED`** —
`award-cached-poll` (the original v1.0 schedule) and `digest-weekly` (v1.2's, `{"mode":
"digest"}` payload) — verified via `terraform state show` and a live AWS query. Nothing has
fired automatically. **Neither schedule should be enabled until all of the "next concrete
steps" in `SESSION_HANDOFF.md` are confirmed clean** — v1.2.1 hasn't even been applied yet,
let alone verified against a real scheduled run.

**The heartbeat bug (real, live in production since the first deploy) is diagnosed AND fixed
in code — but the fix itself is not yet applied.** See the v1.2.1 bullet above for exactly
what changed. Until a real `terraform apply` + a manual invoke confirms a heartbeat datapoint
actually lands, the deployed Lambda is still the OLD, broken code: every real invocation still
ends in `FunctionError`, and the `flight-tracker-app-missed-heartbeat` alarm is still in real
`ALARM` state (continuously since 2026-07-17 — confirmed false, safe to ignore, but still
firing until the real fix ships).

**BWI's exclusion from `DC → Europe (broad)`'s origins — still the right call, but the
original cost-math justification is now stale and hasn't been explicitly re-confirmed.** The
`origins` override was removed entirely (deferred, not abandoned — see `watchlist.yaml`'s own
comment) because a pre-flight check found BWI had never been queried by anything, and adding
it would make an already-wide route fully cache-cold for that origin. That reasoning was
computed against an **economy-only** watchlist; business/first have since tripled the
per-route cabin fan-out, meaning BWI's real incremental cost if re-added is higher than the
original estimate, not lower — the DECISION (stay IAD-only for now) is if anything reinforced
by this, but nobody has gone back and explicitly re-run that math against the current 3-cabin
scope. Not urgent, not blocking anything — just an open confirmation, not a resolved one. See
`SESSION_HANDOFF.md`'s next steps.

**Steady-state real-world cost measurement is incomplete, and now doubly stale.** "Run 1" (a
real `scripts/dry_run.py` pass across both routes, 2026-07-19) produced real numbers — 620.3s
total, 171 SerpApi calls — but that run predates group-winner selection entirely (see
`aws-serverless-deploy`'s "Lambda timeout" section for the exact breakdown, and
`deal-valuation`'s group-winner-selection section for what Run 1's own log revealed: the
entire `max_alerts_per_run` cap consumed by one repeating flat-rate Aeroplan chart). "Run 2"
(meant to run ~20-25 minutes after Run 1, to characterize genuine steady-state cost with most
cash buckets still warm) was never executed — explicitly cancelled mid-session before it
started. Since grouping is expected to change the real cost profile materially (fewer Get
Trips/exact-confirm calls per run), Run 1's pre-grouping numbers shouldn't be treated as the
current steady-state baseline either — a fresh Run 1/Run 2 pair with grouping active is more
informative than finishing the old one. See `SESSION_HANDOFF.md`'s next steps for the
intended order of operations.

## Skill index

| Task | Skill |
|------|-------|
| Query award availability, cached-search + get-trips strategy, rate limits | `seats-aero-integration` |
| Fetch cash fares behind a swappable provider interface | `flight-cash-price-monitor` |
| Decide if a deal is "high value"; CPP math; two-stage cash confirm; dedup + alert-cap design | `deal-valuation` |
| Send/format Discord (default) or Telegram alerts, MarkdownV2 escaping, buttons | `telegram-alerting` |
| Terraform, Lambda, EventBridge, DynamoDB, secrets, CI/OIDC, packaging gotchas, live-testing cost/verification lessons | `aws-serverless-deploy` |
| Handling API keys/webhooks safely: log-leak patterns, cold-start resolution, local/deployed secret sync | `secrets-hygiene` |
| A second entrypoint (dry-run script, alternate CLI) needs production's core logic without reimplementing it | `avoiding-duplicate-implementations` |

When in doubt about an external API's exact current schema, **fetch the live docs** rather
than trusting memory — these providers change endpoints and pricing.
