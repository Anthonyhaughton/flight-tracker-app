# Two access patterns per .claude/skills/aws-serverless-deploy: dedup
# (alerts, TTL-expiring) and cash baselines (no TTL -- these should persist).
# On-demand billing; at this poll volume it's pennies.

resource "aws_dynamodb_table" "alerts" {
  name         = "${var.project_name}-alerts"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "dedup_key"

  attribute {
    name = "dedup_key"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

resource "aws_dynamodb_table" "baselines" {
  name         = "${var.project_name}-baselines"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "route_key"

  attribute {
    name = "route_key"
    type = "S"
  }
}
