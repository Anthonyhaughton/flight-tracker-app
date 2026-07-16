# Zip package, not a container image -- v1.0 is pure API calls (httpx +
# boto3), no headless browser, so this is the smallest/fastest-cold-start
# option. Switch to a container image only if a later phase needs
# Playwright/Chromium (see .claude/skills/flight-cash-price-monitor).

data "aws_ssm_parameter" "seats_aero_api_key" {
  name            = aws_ssm_parameter.seats_aero_api_key.name
  with_decryption = true
}

data "aws_ssm_parameter" "telegram_bot_token" {
  name            = aws_ssm_parameter.telegram_bot_token.name
  with_decryption = true
}

data "aws_ssm_parameter" "telegram_chat_id" {
  name            = aws_ssm_parameter.telegram_chat_id.name
  with_decryption = true
}

resource "aws_cloudwatch_log_group" "poller" {
  name              = "/aws/lambda/${var.project_name}-poller"
  retention_in_days = 30
}

resource "aws_lambda_function" "poller" {
  function_name = "${var.project_name}-poller"
  description   = "Polls seats.aero for award availability and alerts via Telegram on high-value deals."

  filename         = var.lambda_zip_path
  source_code_hash = filebase64sha256(var.lambda_zip_path)

  handler       = "src.poller.lambda_handler"
  runtime       = "python3.12"
  architectures = ["arm64"]

  timeout     = var.lambda_timeout
  memory_size = var.lambda_memory_mb

  role = aws_iam_role.lambda_exec.arn

  environment {
    variables = {
      SEATS_AERO_API_KEY   = data.aws_ssm_parameter.seats_aero_api_key.value
      TELEGRAM_BOT_TOKEN   = data.aws_ssm_parameter.telegram_bot_token.value
      TELEGRAM_CHAT_ID     = data.aws_ssm_parameter.telegram_chat_id.value
      ALERTS_TABLE_NAME    = aws_dynamodb_table.alerts.name
      BASELINES_TABLE_NAME = aws_dynamodb_table.baselines.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.poller]
}
