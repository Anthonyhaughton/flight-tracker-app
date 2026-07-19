variable "project_name" {
  description = "Short name used to prefix all resources."
  type        = string
  default     = "flight-tracker-app"
}

variable "environment" {
  description = "Deployment environment (e.g. prod)."
  type        = string
  default     = "prod"
}

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "lambda_timeout" {
  description = "Poller Lambda timeout, in seconds. Work is I/O-bound, not CPU-bound. 800s (previously 300s, itself previously 120s -- see aws-serverless-deploy's \"Lambda timeout\" section for the full two-raise history). The 300s figure was measured (2026-07-19) against an economy-only watchlist.yaml at 65s total; business/first were re-added to both active routes the SAME session, tripling per-route cabin fan-out, and a fresh real scripts/dry_run.py measurement across BOTH routes immediately afterward took 620.3s total (DC -> Italy 155.22s + DC -> Europe (broad) 465.08s, the latter also spending its first real Get-Trips/exact-confirm calls and exhausting the full max_alerts_per_run cap -- see the skill for the exact call counts). 800s is ~1.3x that 620.3s baseline, stays under Lambda's 900s hard ceiling with 100s to spare, and leaves ~6.7 minutes of margin before the next 20-minute-cadence scheduled invocation (watchlist.yaml's schedule.award_cached_minutes) -- a real, tighter margin than 300s had, not hidden. Group-winner selection (see deal-valuation's winner-selection spec, built the same session as this measurement) is expected to reduce real Get-Trips/exact-confirm call volume going forward; re-measure with grouping active before tuning this number or the cap any further -- see SESSION_HANDOFF.md."
  type        = number
  default     = 800
}

variable "lambda_memory_mb" {
  description = "Poller Lambda memory, in MB."
  type        = number
  default     = 256
}

variable "lambda_zip_path" {
  description = "Path to the built deployment zip (packaged src/ + dependencies + watchlist.yaml)."
  type        = string
  default     = "../dist/poller.zip"
}

variable "award_poll_schedule_expression" {
  description = "EventBridge Scheduler cron/rate expression for the award cached-search poll. rate(1 hour) (previously rate(20 minutes)) -- matches watchlist.yaml's schedule.award_cached_minutes, raised so combined Cached Search + Get Trips cost stays comfortably under seats.aero's 1,000-call/day quota (a real 2026-07-19 measurement found the 20-minute cadence's per-poll cost alone threatened to exceed it). This Terraform variable is the REAL schedule interval -- watchlist.yaml's own value is config-as-code documentation only, not read by Terraform, and had drifted out of sync with it until this change."
  type        = string
  default     = "rate(1 hour)"
}

variable "schedule_enabled" {
  description = "Whether the EventBridge Schedule that triggers the poller is ENABLED or DISABLED. Default false so the first apply creates everything without the schedule firing -- manually invoke the Lambda once to verify a real run succeeds, then apply again with this set to true."
  type        = bool
  default     = false
}

variable "digest_schedule_expression" {
  description = "EventBridge Scheduler cron/rate expression for the weekly digest (see src/digest.py, .claude/skills/deal-valuation). Monday 13:00 UTC by default -- roughly a Monday morning in US Eastern (8/9am depending on DST) for the owner to read at the start of the week."
  type        = string
  default     = "cron(0 13 ? * MON *)"
}

variable "digest_schedule_enabled" {
  description = "Whether the EventBridge Schedule that triggers the weekly digest ({\"mode\": \"digest\"} event, see src/poller.py's run_digest()) is ENABLED or DISABLED. Independent of schedule_enabled above -- same two-phase discipline, its OWN variable: default false so a first apply creates the schedule without it firing, then a manual invoke (`aws lambda invoke ... --payload '{\"mode\":\"digest\"}'`) verifies a real run against production DynamoDB state before enabling it for real."
  type        = bool
  default     = false
}

variable "heartbeat_missing_after_minutes" {
  description = "How long a missing heartbeat metric must persist before the dead-man's-switch alarm fires. Should exceed the poll interval."
  type        = number
  default     = 45
}

variable "alert_email" {
  description = "Email address subscribed to the dead-man's-switch SNS topic."
  type        = string
}

variable "github_repo" {
  description = "GitHub repo allowed to assume the CI/CD OIDC role, as 'owner/name'."
  type        = string
}

variable "terraform_state_bucket" {
  description = "S3 bucket holding Terraform state, used to scope the CI role's state-access permissions."
  type        = string
}

variable "terraform_lock_table" {
  description = "DynamoDB table used for Terraform state locking, used to scope the CI role's permissions."
  type        = string
}
