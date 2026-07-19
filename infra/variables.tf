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
  description = "Poller Lambda timeout, in seconds. Work is I/O-bound, not CPU-bound."
  type        = number
  default     = 120
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
  description = "EventBridge Scheduler cron/rate expression for the award cached-search poll."
  type        = string
  default     = "rate(20 minutes)"
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
