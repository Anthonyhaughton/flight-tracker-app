# Session handoff

**Read `CLAUDE.md` first, then the skill under `.claude/skills/` that matches your task.**
This file is just the "where things actually stand right now" snapshot — it will go stale;
the skills and `CLAUDE.md` are the durable source of truth and were updated alongside this
file to reflect everything below.

## Current state

**v1.0 (award-only) is deployed and confirmed working in production.** A real manual Lambda
invoke has delivered a real, correctly-valued alert to Discord (the default notifier;
Telegram is a swappable alternate — see `telegram-alerting`) from real seats.aero data.
Terraform is applied with a two-phase pattern: the EventBridge schedule was left disabled
until that manual invoke was verified, then enabled.

**v1.1 (real cash comparison) is built and fully tested, but not yet verified live
end-to-end.** `SerpApiClient` implements the real Google Flights schema; cash baselines
(trailing-min + EMA, cached per route+cabin+ISO-week to bound provider call volume) feed a
real effective-CPP gate; a two-stage cash-pricing pattern (cheap weekly estimate decides
candidacy, one real exact-date call confirms finalists before a send — mirroring
seats.aero's Cached-Search-then-Get-Trips shape) is wired into the poller; a second,
independent cash-price-drop trigger exists with its own dedup. 150+ tests pass, all mocked,
zero real network calls.

## What's tested but not yet verified against live data

`scripts/serpapi_smoke_test.py` has been run once for real: one live SerpApi call, real
response parsed cleanly, and the one-way/round-trip directionality guard (the thing this
whole cash integration was most worried about silently getting wrong) held against real
data — no round-trip result slipped through.

That is the *only* piece of v1.1 that has touched live data. The full pipeline — real
baseline caching across multiple candidates, the exact-date confirm step actually rejecting
or confirming a real award, the cash-drop trigger firing (or correctly staying silent) off a
real price change, an actual cash-alert Discord embed landing in the channel — has never run
end-to-end for real. `scripts/dry_run.py` was just fixed for this: it had silently drifted
back to pure v1.0 award-only behavior (missing all the cash wiring above) and additionally
had a latent crash bug in its local baseline store that would have thrown on first use. It
now genuinely mirrors `poll_route()`'s real v1.1 flow, but has not itself been executed yet.

## Next steps

1. **Run `scripts/dry_run.py` live.** One real seats.aero Cached Search plus a small,
   capped number of real SerpApi calls (bounded by `MAX_ALERTS`, same shape as the existing
   seats.aero call budget). Confirm: candidates get correctly cash-gated (an award that
   fails the exact-date confirm must NOT alert), and either a real cash-drop alert lands
   correctly or none does (correctly, if nothing actually dropped).
2. **Before that: confirm `SERPAPI_KEY` is actually set in the deployed SSM parameter
   store**, not just in local `.env` — these do not auto-sync, and a stale/placeholder SSM
   value has already caused one real production 401 for a different key. See
   `secrets-hygiene` for the exact diagnostic steps (length-comparison check) if anything
   401s unexpectedly.
3. **Once `dry_run.py` confirms clean**, do a real production Lambda invoke exercising the
   v1.1 path (same verification pattern already used for v1.0), then update `CLAUDE.md`'s
   Build phases section to mark v1.1 production-verified, and reconsider widening
   `watchlist.yaml` back out from its current deliberately-narrowed safe-testing scope
   (single origin, single route) now that both the per-run alert cap and the two-phase
   apply pattern exist as guardrails.

Nothing in `infra/` needs to change for any of this — the SerpApi SSM parameter and its IAM
grant were already provisioned back in the v1.0 scaffolding, unused until now.
