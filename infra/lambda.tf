# Zip package, not a container image -- v1.0 is pure API calls (httpx +
# boto3), no headless browser, so this is the smallest/fastest-cold-start
# option. Switch to a container image only if a later phase needs
# Playwright/Chromium (see .claude/skills/flight-cash-price-monitor).

resource "aws_cloudwatch_log_group" "poller" {
  name              = "/aws/lambda/${var.project_name}-poller"
  retention_in_days = 30
}

resource "aws_lambda_function" "poller" {
  function_name = "${var.project_name}-poller"
  description   = "Polls seats.aero for award availability and alerts via Discord (default) or Telegram on high-value deals."

  filename         = var.lambda_zip_path
  source_code_hash = filebase64sha256(var.lambda_zip_path)

  handler       = "src.poller.lambda_handler"
  runtime       = "python3.12"
  architectures = ["arm64"]

  timeout     = var.lambda_timeout
  memory_size = var.lambda_memory_mb

  role = aws_iam_role.lambda_exec.arn

  # No secret values here -- only SSM Parameter Store *names* (not
  # sensitive). src/secrets.py resolves the real values via boto3 at cold
  # start (detected via the AWS_LAMBDA_FUNCTION_NAME env var Lambda sets
  # automatically), so decrypted secrets never land in Terraform state or
  # the Lambda console's environment-variables view. See infra/secrets.tf
  # and iam.tf.
  environment {
    variables = {
      SEATS_AERO_API_KEY_SSM_PARAM  = aws_ssm_parameter.seats_aero_api_key.name
      DISCORD_WEBHOOK_URL_SSM_PARAM = aws_ssm_parameter.discord_webhook_url.name
      TELEGRAM_BOT_TOKEN_SSM_PARAM  = aws_ssm_parameter.telegram_bot_token.name
      TELEGRAM_CHAT_ID_SSM_PARAM    = aws_ssm_parameter.telegram_chat_id.name
      SERPAPI_KEY_SSM_PARAM         = aws_ssm_parameter.serpapi_key.name
      ALERTS_TABLE_NAME             = aws_dynamodb_table.alerts.name
      BASELINES_TABLE_NAME          = aws_dynamodb_table.baselines.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.poller]
}
