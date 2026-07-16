# Copy to terraform.tfvars (gitignored) and fill in real values.
# No secrets belong here -- see infra/secrets.tf for how API keys are
# injected out-of-band instead.

aws_region             = "us-east-1"
alert_email            = "you@example.com"
github_repo            = "your-github-username/flight-tracker-app"
terraform_state_bucket = "your-terraform-state-bucket"
terraform_lock_table   = "your-terraform-lock-table"
