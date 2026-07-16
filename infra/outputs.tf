output "lambda_function_name" {
  value = aws_lambda_function.poller.function_name
}

output "alerts_table_name" {
  value = aws_dynamodb_table.alerts.name
}

output "baselines_table_name" {
  value = aws_dynamodb_table.baselines.name
}

output "github_actions_role_arn" {
  description = "Put this in the GitHub Actions workflow's aws-actions/configure-aws-credentials step."
  value       = aws_iam_role.github_actions.arn
}

output "heartbeat_alarm_topic_arn" {
  value = aws_sns_topic.heartbeat_alarm.arn
}
