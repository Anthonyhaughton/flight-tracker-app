# Placeholder SecureString parameters. Real values are set out-of-band after
# apply, e.g.:
#   aws ssm put-parameter --name /flight-deal-agent/seats_aero_api_key \
#     --type SecureString --value "..." --overwrite
# Never through a Terraform variable or .tfvars -- `lifecycle.ignore_changes`
# keeps subsequent applies from clobbering a value set this way.
#
# lambda.tf reads these back via `data "aws_ssm_parameter"` with decryption
# and injects them as the Lambda's environment variables, which means the
# decrypted values do end up in Terraform state -- treat the state backend
# (S3 bucket + DynamoDB lock table) as sensitive: SSE encryption and an IAM
# policy scoped to just this project's deployer. If that tradeoff is
# unacceptable, switch to resolving these inside secrets.py at Lambda
# cold-start via boto3 instead of through Terraform.

resource "aws_ssm_parameter" "seats_aero_api_key" {
  name  = "/${var.project_name}/seats_aero_api_key"
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "telegram_bot_token" {
  name  = "/${var.project_name}/telegram_bot_token"
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "telegram_chat_id" {
  name  = "/${var.project_name}/telegram_chat_id"
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}
