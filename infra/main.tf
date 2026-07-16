terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Remote state in S3 with a DynamoDB lock table -- never local state.
  # Bucket/key/region/lock table are environment-specific and deliberately
  # not hardcoded here; supply them via
  #   terraform init -backend-config=backend.hcl
  # (gitignored, one per environment) rather than committing them.
  backend "s3" {
    encrypt = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
