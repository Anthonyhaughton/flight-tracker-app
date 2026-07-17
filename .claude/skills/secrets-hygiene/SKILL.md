---
name: secrets-hygiene
description: Operational discipline for handling API keys, tokens, and webhook URLs in this codebase — safe cold-start resolution, avoiding accidental leaks into logs, and keeping local and deployed secret stores in sync. Use this whenever the task involves adding a new API key or credential, wiring up a new external integration, debugging an auth failure that "should be working", or reviewing whether a script/module could leak a secret. Reach for it even if the user just says "add a new API key" or "why is this getting a 401".
---

# Secrets hygiene

This is the cross-cutting discipline that sits underneath every integration in this repo
(seats.aero, Discord, Telegram, SerpApi, and whatever comes next) — narrower than
`aws-serverless-deploy` (which covers this specific AWS deployment) and applicable
regardless of which cloud or notifier is in play. Two real incidents in this project trace
back to gaps this skill exists to close: a credential leaked into log output, and a
production auth failure caused by a local secret never being mirrored to the deployed store.

## Pattern: a credential leaking into logs via the request URL

**Not header-based ≠ safe to log.** seats.aero's key is a request header
(`Partner-Authorization`) — httpx's own request logger (`HTTP Request: <method> <full URL>
...`, INFO level by default) is harmless for it, since the header never appears in the
logged line. But **Discord's webhook URL and SerpApi's `api_key` are both part of the
request URL/query string** — the same default httpx logging behavior leaks the literal
credential to stdout/CloudWatch/anywhere logs land, for those.

This is not a one-off bug to fix and forget. It already happened for real once (a Discord
webhook URL leaked into a live dry-run's terminal output and had to be rotated), and it is a
property of *how a given credential is transmitted*, not of any specific provider — so it
will recur with the next integration unless checked for deliberately every time.

**Checklist for every new integration:**
1. Is the credential sent as a request header, or as part of the URL (query string, or the
   URL itself, as with a webhook)?
2. If it's in the URL: silence httpx's own request logger *before* making any real call —
   `logging.getLogger("httpx").setLevel(logging.WARNING)` — in every script/module that
   might log at INFO and could touch this credential. Don't rely on remembering to do this
   per-script; audit every entry point (the poller, smoke-test scripts, dry-run scripts)
   that imports the client.
3. Never print/log the credential directly, even partially — length-only comparisons (see
   below) are safe; substrings or partial reveals are not worth the risk.

If a leak happens anyway: rotate the credential immediately (most providers have a
regenerate/reset action) and treat any log destination it touched as compromised for that
credential specifically, even if you believe the log output wasn't seen by anyone untrusted.

## Pattern: cold-start secret resolution (Lambda)

The actual implemented mechanism (`src/secrets.py` — see `aws-serverless-deploy` for the
Terraform side that provisions it): locally, secrets come straight from environment
variables (a gitignored `.env`). In Lambda, Terraform injects only the SSM parameter *name*
(not the value) as `{VAR}_SSM_PARAM`, and `secrets.py` resolves the real value via `boto3
ssm.get_parameter(WithDecryption=True)` once per cold start, caching it in-process so a warm
invocation never re-hits SSM. One function per secret, two resolution paths, chosen
automatically via the presence of `AWS_LAMBDA_FUNCTION_NAME` — calling code
(`get_seats_aero_api_key()` etc.) never branches on environment itself. A missing required
secret fails loud, naming exactly which environment variable (or SSM parameter path) is
missing and where to get a real value — never a silent fallback to an empty string or a fake
default.

## Pattern: local `.env` and the deployed secret store do not auto-sync

**This caused a real production 401.** Setting `SEATS_AERO_API_KEY` in `.env` only ever
affects local runs (smoke tests, dry runs, `pytest`). It has zero effect on what's
deployed — the Lambda reads from SSM Parameter Store, a completely separate store that
Terraform seeds with a placeholder (`REPLACE_ME`) and deliberately never overwrites on
subsequent applies (`lifecycle.ignore_changes = [value]`, see `aws-serverless-deploy`). It
is entirely possible to have a working, verified local key while the real deployed Lambda is
still silently running on the placeholder — which is exactly what happened: the first real
Lambda invoke 401'd with a key that worked perfectly in every local smoke test, because the
SSM parameter had simply never been manually updated after `.env` was set up.

After rotating or first-setting any secret, mirror it into **both** places explicitly:
1. `.env` (local dev/scripts) — never committed, gitignored.
2. `aws ssm put-parameter --name /flight-tracker-app/<name> --type SecureString --value "..." --overwrite`
   (the deployed Lambda) — never through Terraform (see `aws-serverless-deploy`'s reasoning
   on why decrypted values must never pass through Terraform state/plan).

**When diagnosing an auth failure that "should be working,"** check the cheapest, most
specific explanation first, in this order:
1. Does the exact same key work in a local smoke test at all? (Rules out the key itself or
   the provider account being the problem.)
2. Does the deployed store's value's **length** match the local value's length? A quick,
   secret-safe check — compare `len()` of each, never the values themselves. A mismatch
   usually means a stale/placeholder value (e.g. still `REPLACE_ME`, 10 characters) or stray
   whitespace/a newline introduced when pasting into `put-parameter`.
3. Only after both of those check out, suspect the account/provider side (revoked key, plan
   downgrade, region restriction).

Skipping straight to "the provider must be having issues" or "let me just re-paste the key
and hope" wastes time versus this cheap, ordered diagnostic — length comparison alone has
already caught a real "still the Terraform placeholder" bug in this project.
