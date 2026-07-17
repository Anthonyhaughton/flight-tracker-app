---
name: aws-serverless-deploy
description: Deploy the deal agent cost-effectively on AWS serverless with Terraform — EventBridge-scheduled Lambda, DynamoDB for dedup/baselines, SSM/Secrets Manager for keys, CloudWatch logging, a dead-man's-switch heartbeat, and GitHub Actions CI using OIDC (no long-lived keys). Use this whenever the task involves hosting, scheduling (cron/event-driven), infrastructure-as-code, Terraform, Lambda, DynamoDB, secrets management, IAM least-privilege, CI/CD, or "how do I deploy/run this in the cloud". Reach for it even if the user just says "get this running on a schedule" or "deploy it".
---

# AWS serverless deployment

This is a low-frequency, bursty, embarrassingly-schedulable workload — textbook serverless,
and effectively free at this volume. Owner has a DevSecOps background, so favor clean IaC,
least privilege, and no long-lived credentials over hand-clicking the console.

**Swap point:** everything here lives in `infra/` (Terraform) and a thin `deploy/` layer.
If the owner wants GCP (Cloud Scheduler → Cloud Run job → Firestore) or a plain VPS
(systemd timer + SQLite), only these layers change; the Python is cloud-agnostic.

## Topology

```
EventBridge Scheduler ──(cron)──▶ Lambda (poller) ──▶ DynamoDB (dedup + baselines)
                                       │
                                       ├──▶ SSM Parameter Store / Secrets Manager (keys)
                                       ├──▶ CloudWatch Logs
                                       └──▶ external APIs (seats.aero, SerpApi, Telegram)

CloudWatch Alarm (missed heartbeat) ──▶ SNS ──▶ owner   # dead-man's switch
```

## Lambda packaging

- **Zip package** is fine if the poller is pure API calls (httpx + boto3). Smallest, fastest
  cold start. This is the v1 default since we don't scrape in v1.
- **Container image** (up to 10GB) only if a later phase needs Playwright/Chromium. Don't
  reach for it prematurely.
- Runtime Python 3.12, arm64 (cheaper). Set a generous timeout (e.g., 120s) and modest memory
  (256–512MB); the work is I/O-bound.

**Packaging gotcha: cross-platform wheels, not the host machine's.** Building the
deployment zip on a Mac (or any non-Lambda platform) with a plain `pip install --target`
will, for any dependency with a compiled extension (this project: PyYAML's `_yaml` C
binding), install a **Mach-O** binary that Lambda's Linux runtime cannot load. This fails at
Lambda *import time*, not at build time locally — so a bad zip looks completely fine right
up until the first real invocation. Force pip to fetch prebuilt Linux wheels for the exact
target instead of ever building/linking locally:

```
pip install --target build/ \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  -r requirements.txt
```

`--only-binary=:all:` is the critical flag — without it, pip can silently fall back to
building an sdist locally (against the host's own libc/architecture) for any package
lacking a matching manylinux wheel, quietly reintroducing the exact bug the other three
flags exist to prevent. Verify the built artifact before trusting it: `unzip -l` for any
`.so` files, then `file` on the extracted binary should read `ELF ... ARM aarch64`, never
`Mach-O`. If Docker is available, the authoritative check is running the actual Lambda base
image against the built zip (`public.ecr.aws/lambda/python:3.12-arm64`) and importing every
dependency before ever deploying it — a static architecture check confirms the binary
*format* is right, not that it actually *runs* cleanly inside the runtime (missing shared
libs, glibc mismatches, etc. wouldn't show up in a static check).

## Scheduling

EventBridge Scheduler with a cron/rate expression. Cadence guidance (also see the individual
skills' rate-limit notes):

- Award cached poll: every 15–30 min.
- Cash baseline refresh: hourly.
- Stagger routes across invocations rather than doing everything every run, to stay under
  provider quotas. A single Lambda that iterates a slice of `watchlist.yaml` per run, or a
  couple of schedules with different inputs, both work.

## First deploy: two-phase apply (verify before the schedule can fire)

Don't let the very first `terraform apply` immediately start invoking a Lambda that hasn't
been verified against real integrations yet — an untested poller with an empty dedup table
and a wide watchlist can fan out into many simultaneous real alerts on its first scheduled
run (see the 73-alert flood in `deal-valuation`'s dedup section). Add a `schedule_enabled`
Terraform variable (default `false`) mapping to the `aws_scheduler_schedule` resource's
`state` argument (`"ENABLED"`/`"DISABLED"`):

1. Apply once with `schedule_enabled = false` — everything gets created (Lambda, IAM,
   DynamoDB, SSM parameters, the schedule itself) but the schedule won't fire.
2. Set the real secret values in SSM (`aws ssm put-parameter --overwrite`, see
   `secrets-hygiene`) and manually invoke the Lambda once (`aws lambda invoke` or the
   console). Confirm it succeeds — check CloudWatch Logs and that a heartbeat metric landed.
3. Apply again with `schedule_enabled = true` to turn on the real cadence.

Combine this with a deliberately narrowed `watchlist.yaml` (fewer origins/routes, a tighter
date window, commented as "narrowed for safe first-production verification — widen after a
confirmed run") for that first verified invoke specifically, and with the
`max_alerts_per_run` cap (`deal-valuation`) as defense in depth — the two-phase apply
prevents an *automatic* flood; the cap prevents a flood on a *manual* invoke too.

## DynamoDB

Two access patterns, one or two tables:

- **Alerts (dedup):** partition key = dedup key (from `deal-valuation`), with a **TTL
  attribute** (3–7 days) so vanished-then-returned deals can re-alert but standing deals
  don't nag. Enable TTL on that attribute.
- **Baselines:** partition key = route/cabin/date-bucket key, holds trailing-min + EMA for
  cash drop detection.

On-demand (pay-per-request) billing — at this volume it's pennies. No provisioned capacity.

**Table names must flow from the real Terraform resource, never a hardcoded string in
application code.** This shipped as a real bug once: `poller.py` hardcoded
`DynamoStateStore(alerts_table="flight-deal-alerts", ...)` — a stale name from before the
project was renamed — while `infra/lambda.tf` correctly set `ALERTS_TABLE_NAME` from the
real `aws_dynamodb_table.alerts.name` (`flight-tracker-app-alerts`). The application code
simply never read the env var Terraform was already providing correctly. The failure mode
is a deceptive `AccessDeniedException` on `dynamodb:GetItem`, not a
`ResourceNotFoundException` — IAM correctly denies a table name nobody ever granted access
to, which reads like a permissions bug and sends you looking in the wrong place first (IAM
policy, not the app code) before you realize the table name itself is wrong. Read table (and
any other Terraform-provisioned resource) names from the environment at runtime, fail loud
with a clear message naming the exact missing variable if it's absent, and add a test
asserting the real env var name is what's actually read — a hardcoded default is a silent
regression waiting to happen, especially across a project rename.

## Secrets & config

**Implemented, not just proposed** (`src/secrets.py`, `infra/secrets.tf`, `infra/lambda.tf`,
`infra/iam.tf`). API keys (`SEATS_AERO_API_KEY`, `DISCORD_WEBHOOK_URL`, `SERPAPI_KEY`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) live in SSM Parameter Store (SecureString).
Terraform never injects the decrypted *values* as Lambda environment variables — that would
land decrypted secrets in Terraform state, plan output, and the Lambda console's
environment-variables view. Instead each secret's SSM parameter **name** (not sensitive,
just a path like `/flight-tracker-app/seats_aero_api_key`) is injected as `{VAR}_SSM_PARAM`
(e.g. `SEATS_AERO_API_KEY_SSM_PARAM`), and `secrets.py` resolves the real value via
`boto3 ssm.get_parameter(WithDecryption=True)` at cold start — detected via the
`AWS_LAMBDA_FUNCTION_NAME` env var Lambda sets automatically — caching it in-process for the
lifetime of the warm container so a warm invocation never re-hits SSM. Locally (that env var
absent), the same functions read the env var directly from a gitignored `.env` instead. One
function per secret, two resolution paths, chosen automatically — calling code never
branches on environment itself.

See `secrets-hygiene` for the operational discipline around this (log-leak patterns,
`.env`/SSM drift) that applies beyond this AWS-specific mechanism — including a real
production incident it would have prevented.

- `watchlist.yaml` ships in the deployment package (it's config, not secret). Editing routes
  = redeploy the package (fast) or, later, store it in S3/Parameter Store for hot edits.

## IAM (least privilege — no wildcards)

The Lambda execution role gets exactly:

- `dynamodb:GetItem/PutItem/UpdateItem/Query` on the specific table ARNs.
- `ssm:GetParameter(s)` on the specific parameter ARNs (or `secretsmanager:GetSecretValue`
  on specific secret ARNs), plus `kms:Decrypt` on the CMK if using one.
- `logs:CreateLogStream/PutLogEvents` on its own log group.

Nothing on `*`. Scope every statement to named resources.

## Dead-man's switch (do not skip)

"No alerts" is ambiguous between "no deals" and "the poller is dead." Make silence
meaningful: have each successful run emit a CloudWatch custom metric (or ping
Healthchecks.io), and set a CloudWatch **Alarm** that fires to SNS → the owner if the metric
is missing for longer than the poll interval. Cheap insurance against silent failure.

## CI/CD — GitHub Actions with OIDC (no stored AWS keys)

Do not put AWS access keys in GitHub secrets. Use **OIDC**: configure an IAM role trusting
GitHub's OIDC provider, scoped to this repo, and have the workflow assume it with
`aws-actions/configure-aws-credentials`. Pipeline: lint + tests → `terraform plan` on PR →
`terraform apply` on merge to main → package & update the Lambda.

- Remote Terraform state in S3 with a DynamoDB lock table; never local state.
- Gate `apply` behind the main branch / an environment protection rule.
- Run `terraform fmt -check`, `validate`, and (bonus, given DevSecOps) `tfsec`/`checkov` in
  CI.

## Cost sanity

At 15–30 min polling, this sits comfortably inside the Lambda free tier, DynamoDB on-demand
costs pennies, and Parameter Store standard params are free. The real recurring spend is the
**seats.aero Pro subscription and the SerpApi plan** — the AWS bill is rounding error.

## Terraform module sketch

```
infra/
├── main.tf              # provider, backend (S3 + Dynamo lock)
├── lambda.tf            # function, role, log group
├── schedule.tf          # EventBridge Scheduler
├── dynamodb.tf          # alerts (TTL) + baselines tables
├── secrets.tf           # SSM SecureString params (values injected out-of-band)
├── monitoring.tf        # heartbeat metric alarm + SNS topic
├── iam.tf               # least-privilege policies + GitHub OIDC role
└── variables.tf / outputs.tf
```

Inject secret *values* out-of-band (CLI/console), not through Terraform variables, so they
never land in state or plan output.
