# EventBridge Scheduler (not the older EventBridge Rules) invokes the
# poller on the award cached-search cadence, AND (see below) the weekly
# digest -- both schedules share the SAME scheduler IAM role/policy and
# target the SAME Lambda function, distinguished only by their `input`
# event payload (src/poller.py's lambda_handler dispatches on event.mode).

data "aws_iam_policy_document" "scheduler_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.project_name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume_role.json
}

# Scoped to the Lambda FUNCTION's ARN, not to any particular schedule or
# event payload -- lambda:InvokeFunction at the function level authorizes
# every aws_scheduler_schedule below that uses this same role and targets
# this same function ARN, regardless of how many schedules exist or what
# `input` each one sends. Verified (not assumed) when the digest schedule
# was added below: no new statement, resource, or role was needed for it.
data "aws_iam_policy_document" "scheduler_invoke_lambda" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.poller.arn]
  }
}

resource "aws_iam_role_policy" "scheduler_invoke_lambda" {
  name   = "${var.project_name}-scheduler-invoke"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler_invoke_lambda.json
}

resource "aws_scheduler_schedule" "award_cached_poll" {
  name                         = "${var.project_name}-award-cached-poll"
  schedule_expression          = var.award_poll_schedule_expression
  schedule_expression_timezone = "UTC"
  state                        = var.schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.poller.arn
    role_arn = aws_iam_role.scheduler.arn
    # No `input` -- omitting it sends an empty payload, which
    # lambda_handler's event.get("mode") == "digest" check correctly reads
    # as "not digest mode", i.e. the existing real-time poll path.
  }
}

# Second, independent schedule for the weekly digest (src/digest.py,
# src/poller.py's run_digest()) -- same Lambda, same scheduler role/policy
# (see the comment on scheduler_invoke_lambda above), distinguished only by
# the {"mode": "digest"} event payload. Its own disabled-by-default variable
# (digest_schedule_enabled) -- same two-phase discipline as schedule_enabled
# above, but independent: enabling the award-poll schedule must never
# silently also enable the digest, or vice versa.
resource "aws_scheduler_schedule" "digest_weekly" {
  name                         = "${var.project_name}-digest-weekly"
  schedule_expression          = var.digest_schedule_expression
  schedule_expression_timezone = "UTC"
  state                        = var.digest_schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.poller.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ mode = "digest" })
  }
}
