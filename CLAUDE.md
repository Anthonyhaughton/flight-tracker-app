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
when it is **saver-priced**, in a **cabin the owner cares about** (business/first for
long-haul), and its **effective cents-per-point beats the owner's floor for that program**
— or when a cash fare drops meaningfully below its tracked baseline. Availability alone is
never the trigger. The cash price behind that CPP math is itself two-stage (a cheap,
cached weekly estimate decides candidacy; one precise real call confirms the exact date
before a real alert sends) — see `deal-valuation`.

## Build phases (ship v1 before touching the hard part)

Prove the pipeline end-to-end on the *clean* data source first, then add the part where
all the anti-bot pain lives.

- **v1.0 — award-only. ✅ DEPLOYED and confirmed working end-to-end in production.**
  seats.aero cached search → cabin filter (business/first) → valuation gate → dedup →
  Discord (default notifier; Telegram is a swappable alternate impl, see
  `telegram-alerting`). Verified via a real manual Lambda invoke: real seats.aero data →
  real valuation → a real Discord alert delivered. Deployed via Terraform using a two-phase
  apply (schedule created disabled, verified with one manual invoke, then enabled — see
  `aws-serverless-deploy`).
- **v1.1 — cash + real valuation. ✅ PRODUCTION-VERIFIED end-to-end against live data.**
  `CashFareProvider` (SerpApi) implemented against the live API reference; cash baselines
  (trailing-min + EMA, ISO-week-bucketed to bound provider call volume) with a real
  exact-date confirm step for finalists before a real alert sends; real effective-CPP
  gating (`comparable_cash_usd` is no longer always `None`); a second, independent
  cash-price-drop trigger. 150+ tests pass, all mocked, zero real network. Live-verified via
  `scripts/dry_run.py` in both directions: a real run sent a real Discord award alert with a
  real confirmed price/CPP in the footer (not the v1.0 "no cash comparison yet" placeholder);
  a follow-up run with `cpp_floors` deliberately inflated to an unreachable value confirmed
  the real CPP gate correctly rejects — 10/10 real candidates skipped with the expected
  `"X.Xcpp below PROGRAM floor"` reason, matching the mocked suite's format exactly. Dedup
  confirmed to record state only on an actual send, never on a valuation-rejected candidate.
  The cash-drop trigger correctly stayed silent on cold-start baselines (seeds silently,
  never alerts on first observation); a live warm-baseline drop firing for real is still
  unobserved, but is expected to need multiple runs over time to occur naturally and is not a
  blocker for calling v1.1 closed.
- **v1.2 — controls.** Inline-keyboard mute/snooze, `watchlist.yaml` fully drives routes,
  heartbeat alarm.
- **v2 — breadth.** "Anywhere in Europe" inspiration search, more programs, mistake-fare
  detection.

v1.1 is production-verified: `scripts/dry_run.py` has delivered a real, correctly
cash-gated award alert to the owner's phone, and a separate run confirmed the real CPP gate
correctly rejects real candidates that fail the floor.

## Skill index

| Task | Skill |
|------|-------|
| Query award availability, cached-search + get-trips strategy, rate limits | `seats-aero-integration` |
| Fetch cash fares behind a swappable provider interface | `flight-cash-price-monitor` |
| Decide if a deal is "high value"; CPP math; two-stage cash confirm; dedup + alert-cap design | `deal-valuation` |
| Send/format Discord (default) or Telegram alerts, MarkdownV2 escaping, buttons | `telegram-alerting` |
| Terraform, Lambda, EventBridge, DynamoDB, secrets, CI/OIDC, packaging gotchas | `aws-serverless-deploy` |
| Handling API keys/webhooks safely: log-leak patterns, cold-start resolution, local/deployed secret sync | `secrets-hygiene` |

When in doubt about an external API's exact current schema, **fetch the live docs** rather
than trusting memory — these providers change endpoints and pricing.
