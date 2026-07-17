# infra

Terraform for the poller Lambda, its DynamoDB tables, EventBridge Schedule,
SSM secret parameters, IAM roles, and the heartbeat alarm. See `CLAUDE.md` at
the repo root for the overall architecture.

## Before every plan/apply: build the Lambda package

Terraform does not build the deployment zip itself -- there's no
`null_resource` invoking pip/zip. Run the build script first, from the repo
root, with the project's `.venv` activated:

```
source .venv/bin/activate
scripts/build_lambda_package.sh
```

This installs `src/`'s runtime dependencies (from `pyproject.toml`, matching
the Lambda's Python 3.12 / arm64 runtime) into a clean build directory,
copies in `src/` and `watchlist.yaml`, and zips the result to
`dist/poller.zip` -- the path `lambda_zip_path` points at by default.
`lambda.tf` hashes that file (`filebase64sha256`) so a new package
automatically triggers a Lambda code update on the next apply.

Re-run the build script (then re-plan/apply) any time `src/`,
`watchlist.yaml`, or `pyproject.toml`'s dependencies change.

## First deploy: apply with the schedule disabled

`schedule_enabled` (in `variables.tf`) defaults to `false`, which sets the
EventBridge Schedule's `state` to `DISABLED`. For a first deploy:

1. `terraform apply` with the default (`schedule_enabled = false`). Everything
   gets created -- Lambda, IAM, DynamoDB, SSM parameters, the schedule itself
   -- but the schedule won't fire.
2. Set the real secret values in SSM (see `secrets.tf`'s header comment for
   the `aws ssm put-parameter` commands -- never through a `.tfvars` file).
3. Manually invoke the Lambda once (`aws lambda invoke --function-name
   flight-tracker-app-poller ...` or via the console) and confirm it succeeds
   -- check CloudWatch Logs and that a heartbeat metric landed.
4. Once verified, apply again with `schedule_enabled = true` (either edit
   `terraform.tfvars` or pass `-var schedule_enabled=true`) to turn on the
   real cadence.
