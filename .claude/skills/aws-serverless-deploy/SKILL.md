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

## Scheduling

EventBridge Scheduler with a cron/rate expression. Cadence guidance (also see the individual
skills' rate-limit notes):

- Award cached poll: every 15–30 min.
- Cash baseline refresh: hourly.
- Stagger routes across invocations rather than doing everything every run, to stay under
  provider quotas. A single Lambda that iterates a slice of `watchlist.yaml` per run, or a
  couple of schedules with different inputs, both work.

## DynamoDB

Two access patterns, one or two tables:

- **Alerts (dedup):** partition key = dedup key (from `deal-valuation`), with a **TTL
  attribute** (3–7 days) so vanished-then-returned deals can re-alert but standing deals
  don't nag. Enable TTL on that attribute.
- **Baselines:** partition key = route/cabin/date-bucket key, holds trailing-min + EMA for
  cash drop detection.

On-demand (pay-per-request) billing — at this volume it's pennies. No provisioned capacity.

## Secrets & config

- API keys (`SEATS_AERO_API_KEY`, `SERPAPI_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)
  live in **SSM Parameter Store (SecureString)** or Secrets Manager. `secrets.py` reads them
  at cold start and caches for the invocation. Parameter Store is cheaper for a handful of
  values; Secrets Manager adds rotation you don't need yet.
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
