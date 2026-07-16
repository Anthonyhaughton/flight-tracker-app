locals {
  heartbeat_namespace = "${var.project_name}/Heartbeat"
  heartbeat_metric    = "PollSucceeded"
}

resource "aws_sns_topic" "heartbeat_alarm" {
  name = "${var.project_name}-heartbeat-alarm"
}

resource "aws_sns_topic_subscription" "heartbeat_alarm_email" {
  topic_arn = aws_sns_topic.heartbeat_alarm.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# The poller emits PutMetricData(PollSucceeded, 1) at the end of every clean
# run (see CloudWatchHeartbeat in src/poller.py) -- but only on success, so
# an unhandled error (auth failure, etc.) shows up here as missing data
# rather than a silent "no alerts today." Dead-man's-switch per CLAUDE.md's
# "fail loud, not silent" convention.
resource "aws_cloudwatch_metric_alarm" "heartbeat" {
  alarm_name          = "${var.project_name}-missed-heartbeat"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = local.heartbeat_metric
  namespace           = local.heartbeat_namespace
  period              = var.heartbeat_missing_after_minutes * 60
  statistic           = "Sum"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_description   = "No successful poller run in the last ${var.heartbeat_missing_after_minutes} minutes."
  alarm_actions       = [aws_sns_topic.heartbeat_alarm.arn]
}
