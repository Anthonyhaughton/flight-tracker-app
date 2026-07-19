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

## What counts as "high value"

Defined in full in the `deal-valuation` skill. In one line: an award is worth alerting on
when it is **saver-priced**, in a **cabin the owner cares about** (a `watchlist.yaml`
per-route config choice — both active routes currently watch **economy**, not the original
business/first; see `deal-valuation`'s real validated economy-cabin calibration), its
**program is one the owner can actually book through** (Amex MR / Chase UR transfer
partners — `eligible_programs`, see `deal-valuation`), and its **effective cents-per-point
beats the owner's floor for that program** — or when a cash fare drops meaningfully below
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
  `aws-serverless-deploy`). **This is still the only code actually running in production —
  see "Current deploy status" below.**
- **v1.1 — cash + real valuation. ✅ BUILT, TESTED, AND LOCALLY LIVE-VERIFIED. ⚠️ NEVER
  DEPLOYED.** `CashFareProvider` (SerpApi) implemented against the live API reference; cash
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
  fallback-direction fix, shared-logic refactor. ✅ BUILT, TESTED, LOCALLY LIVE-VERIFIED.
  ⚠️ NEVER DEPLOYED.** Everything in this phase is real, working, and has never touched the
  deployed Lambda:
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
  - A `terraform plan` has been reviewed (source-code-hash-only diff, confirmed clean) but
    **never applied**.
- **v1.2 — the weekly digest. ✅ BUILT AND TESTED (mocked only). ⚠️ NOT YET LIVE-VERIFIED.
  ⚠️ NEVER DEPLOYED.** Same status tier v1.1.1 passed through before its own live
  verification — see `deal-valuation` for the full implementation detail. `src/digest.py`'s
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
  and into `scripts/dry_run.py` via `--mode digest`. 18 new tests, all mocked, full suite
  green. **Not yet run against real live data** — `--mode digest` has only been smoke-tested
  with a fake key (confirmed it reaches a real seats.aero call and fails loudly on auth,
  proving the wiring, not the ranking output). A real `scripts/dry_run.py --mode digest` run
  against live seats.aero/SerpApi/Discord is the next concrete step, mirroring how v1.1 was
  live-verified before v1.1.1 was ever considered for deploy — see `SESSION_HANDOFF.md`. No
  Terraform/infra work has been done for this phase at all yet (the second EventBridge
  schedule doesn't exist yet, even in an unreviewed plan) — that's a separate, later step
  after live verification, not started.
- **v1.3 — further controls.** Inline-keyboard mute/snooze, heartbeat alarm tuning.
- **v2 — breadth.** "Anywhere in Europe" inspiration search, more programs, mistake-fare
  detection.

## Current deploy status (read this before assuming anything is live)

**The deployed Lambda is still running pre-v1.1 code** — award-only, business/first cabin,
no `eligible_programs` filter, no economy support, the original 1.3–1.5cpp/$1,500 thresholds,
and the old permissive cash-outage fallback (now known to be a real safety issue — see
`deal-valuation`). None of v1.1 or v1.1.1 above is live. The EventBridge schedule has
**never been enabled** at any point in this project's history (`schedule_enabled` has been
`false` since the very first deploy) — the Lambda has never fired on its own schedule, only
via manual invokes during v1.0 verification.

Closing this gap requires two more deliberate, reviewed steps, mirroring the v1.0 two-phase
pattern: apply the already-reviewed Terraform plan (ships the new code, schedule stays off),
then a second apply flipping `schedule_enabled` to `true`.

**One more real gap inside the current config itself:** `watchlist.yaml`'s real
`DC → Europe (broad)` route sets `origins: [IAD, BWI]` — but only `IAD` has ever actually been
queried by anything (real API calls or local dry runs). `BWI` is configured and would be
included the moment this deploys, but its real behavior (does it surface different programs?
different density?) is completely unverified.

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
