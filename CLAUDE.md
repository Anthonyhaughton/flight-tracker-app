# CLAUDE.md — Flight & Award Deal Agent

This file is the operating brief for any agent working in this repo. Read it fully
before writing code. When a task maps to one of the skills in `.claude/skills/`,
consult that skill first — it holds the detailed, current guidance.

## Mission

Monitor **cash flight prices** and **award (points/miles) availability** out of the
Washington D.C. airports (IAD, DCA, BWI) toward Europe (Italy first), and push a
**Telegram alert only when a genuinely high-value deal appears.** High-value is a
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
                 │  │ seats-aero     │──┼──▶ (cached poll → live confirm)
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
                 │  │ telegram send  │──┼──▶ Bot API
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
| Notifications      | Telegram Bot API                | `Notifier` interface |

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
│   ├── valuation.py          # CPP + thresholds → "high value?" decision
│   ├── state.py              # StateStore interface + DynamoDB impl (dedup, baselines)
│   ├── notify/
│   │   ├── base.py           # Notifier interface
│   │   └── telegram.py       # default impl
│   ├── config.py             # loads watchlist.yaml + CPP valuations
│   └── secrets.py            # pulls API keys from SSM/Secrets Manager
├── infra/                    # Terraform
├── tests/
└── .claude/skills/           # the skills below
```

## Non-negotiable conventions

- **Secrets never touch git.** `SEATS_AERO_API_KEY`, `SERPAPI_KEY`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID` load at runtime from SSM/Secrets Manager (locally: `.env`, which is
  gitignored). No key literals in code, tests, or Terraform state. Verify `.gitignore`
  covers `.env` and `*.tfvars` before the first commit.
- **Config as code.** Routes, cabins, date windows, per-program CPP valuations, and alert
  thresholds live in `watchlist.yaml`, not in code. Adding a route is a config edit, not
  a deploy of new logic.
- **Dedup is mandatory.** Every alert path goes through the state store. Never send a
  Telegram message without first checking + recording a dedup key. See `deal-valuation`
  for the key design.
- **Respect rate limits.** seats.aero has a daily quota that resets at 00:00 UTC. Poll the
  *cached* endpoint frequently and cheaply; spend a *live* search only to confirm a hit
  right before alerting. Back off on 429s; never hammer.
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
never the trigger.

## Build phases (ship v1 before touching the hard part)

Prove the pipeline end-to-end on the *clean* data source first, then add the part where
all the anti-bot pain lives.

- **v1.0 — award-only.** seats.aero cached search → saver business-class filter on 2–3
  routes → valuation gate → dedup → Telegram. No cash scraping yet. Deployed via Terraform.
- **v1.1 — cash + real valuation.** Add the `CashFareProvider` (SerpApi), track cash
  baselines, compute true effective-CPP by comparing award cost against live cash.
- **v1.2 — controls.** Inline-keyboard mute/snooze, `watchlist.yaml` fully drives routes,
  heartbeat alarm.
- **v2 — breadth.** "Anywhere in Europe" inspiration search, more programs, mistake-fare
  detection.

Do not start v1.1 until v1.0 delivers a real alert to the owner's phone.

## Skill index

| Task | Skill |
|------|-------|
| Query award availability, cached-vs-live strategy, rate limits | `seats-aero-integration` |
| Fetch cash fares behind a swappable provider interface | `flight-cash-price-monitor` |
| Decide if a deal is "high value"; CPP math; dedup key design | `deal-valuation` |
| Send/format Telegram alerts, MarkdownV2 escaping, buttons | `telegram-alerting` |
| Terraform, Lambda, EventBridge, DynamoDB, secrets, CI/OIDC | `aws-serverless-deploy` |

When in doubt about an external API's exact current schema, **fetch the live docs** rather
than trusting memory — these providers change endpoints and pricing.
