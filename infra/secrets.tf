# Placeholder SecureString parameters. Real values are set out-of-band after
# apply, e.g.:
#   aws ssm put-parameter --name /flight-tracker-app/seats_aero_api_key \
#     --type SecureString --value "..." --overwrite
# Never through a Terraform variable or .tfvars -- `lifecycle.ignore_changes`
# keeps subsequent applies from clobbering a value set this way.
#
# These values are NEVER read back into Terraform (no `data
# "aws_ssm_parameter"`, no injection into the Lambda's environment block) --
# the decrypted secret never touches Terraform state or plan output. Instead
# src/secrets.py resolves them at Lambda cold start via boto3
# ssm.get_parameter(WithDecryption=true), given only the parameter *name*
# (not sensitive) via {VAR}_SSM_PARAM env vars set in lambda.tf. See iam.tf
# for the matching ssm:GetParameter + kms:Decrypt grant.

resource "aws_ssm_parameter" "seats_aero_api_key" {
  name  = "/${var.project_name}/seats_aero_api_key"
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "discord_webhook_url" {
  name  = "/${var.project_name}/discord_webhook_url"
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

# Not read by any code yet (v1.1 cash-fare provider) -- created now so the
# IAM grant and Lambda wiring don't need a second infra change when it lands.
resource "aws_ssm_parameter" "serpapi_key" {
  name  = "/${var.project_name}/serpapi_key"
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}
