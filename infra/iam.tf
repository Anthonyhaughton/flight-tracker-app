# Least privilege throughout -- every statement is scoped to named resource
# ARNs, nothing on "*" except where the AWS API genuinely has no
# resource-level permission support (noted inline).

# SSM SecureString parameters (infra/secrets.tf) use the AWS-managed default
# key (alias/aws/ssm) since no key_id was specified on those resources --
# this looks up its real ARN so the Decrypt grant below can be scoped to it
# specifically, rather than every KMS key in the account.
data "aws_kms_alias" "ssm_default" {
  name = "alias/aws/ssm"
}

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_exec" {
  name               = "${var.project_name}-poller-exec"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "lambda_permissions" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.poller.arn}:*"]
  }

  statement {
    sid = "Dynamo"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
    ]
    resources = [
      aws_dynamodb_table.alerts.arn,
      aws_dynamodb_table.baselines.arn,
    ]
  }

  statement {
    sid     = "Ssm"
    actions = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = [
      aws_ssm_parameter.seats_aero_api_key.arn,
      aws_ssm_parameter.discord_webhook_url.arn,
      aws_ssm_parameter.telegram_bot_token.arn,
      aws_ssm_parameter.telegram_chat_id.arn,
      aws_ssm_parameter.serpapi_key.arn,
    ]
  }

  statement {
    sid       = "SsmDecrypt"
    actions   = ["kms:Decrypt"]
    resources = [data.aws_kms_alias.ssm_default.target_key_arn]
  }

  statement {
    sid       = "Heartbeat"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"] # CloudWatch PutMetricData has no resource-level ARNs; scoped by namespace instead
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [local.heartbeat_namespace]
    }
  }
}

resource "aws_iam_role_policy" "lambda_exec" {
  name   = "${var.project_name}-poller-exec"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_permissions.json
}

# --- CI/CD: GitHub Actions via OIDC, no long-lived AWS keys ---

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

data "aws_iam_policy_document" "github_oidc_assume_role" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:*"]
    }
  }
}

resource "aws_iam_role" "github_actions" {
  name               = "${var.project_name}-github-actions"
  assume_role_policy = data.aws_iam_policy_document.github_oidc_assume_role.json
}

# CI needs to plan/apply this Terraform config and push new Lambda code.
# Scoped to exactly the resources this project owns.
data "aws_iam_policy_document" "github_actions_permissions" {
  statement {
    sid = "LambdaDeploy"
    actions = [
      "lambda:UpdateFunctionCode",
      "lambda:GetFunction",
      "lambda:UpdateFunctionConfiguration",
    ]
    resources = [aws_lambda_function.poller.arn]
  }

  statement {
    sid       = "TerraformState"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["arn:aws:s3:::${var.terraform_state_bucket}/*"]
  }

  statement {
    sid       = "TerraformLock"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
    resources = ["arn:aws:dynamodb:${var.aws_region}:*:table/${var.terraform_lock_table}"]
  }
}

resource "aws_iam_role_policy" "github_actions" {
  name   = "${var.project_name}-github-actions"
  role   = aws_iam_role.github_actions.id
  policy = data.aws_iam_policy_document.github_actions_permissions.json
}
