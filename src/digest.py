"""Weekly digest: a snapshot-at-digest-time ranking of every candidate seen
this run, sent as ONE aggregate message regardless of whether anything
clears the real-time bar (see .claude/skills/deal-valuation's digest spec).

Shared vs new, per .claude/skills/avoiding-duplicate-implementations -- this
is a "shared lower layer, different upper orchestration" case, not a
wholesale-reuse case:

- SHARED (imported, not reimplemented): `passes_award_prefilter` and
  `compute_effective_cpp` (src/valuation.py -- the exact same eligible-
  programs/cabin gate and CPP math src/poller.py's evaluate_candidate() and
  scripts/dry_run.py already use), `is_high_value` (src/valuation.py -- used
  here for its real fire/skip verdict against CONFIRMED numbers, to answer
  "would this have cleared the real-time bar" without re-deriving that
  condition), and `get_or_refresh_baseline`/`confirm_exact_date_price`
  (src/cash.py -- the SAME two-stage cheap-estimate-then-real-confirm cash
  pricing the real-time path uses).
- NEW (this module): the actual ranking/aggregation orchestration --
  `build_weekly_digest()` walks every active route, ranks ALL surviving
  candidates (not just gate-passers) by the cheap weekly-bucketed estimate,
  selects two independent top-5 lists, and spends a real exact-date confirm
  call only on the union of those two lists' finalists (deduped, so a
  candidate appearing in both lists costs one call, not two).

Deliberately does NOT call src/poller.py's evaluate_candidate(): that
function's shape is "one candidate, gate it, maybe send one alert, dedup +
per-run cap apply" -- none of which fits a digest that ranks EVERY candidate
and sends exactly one aggregate message, with no dedup and no cap. Forcing
evaluate_candidate() to serve both shapes would mean either a digest that
silently drops candidates it doesn't gate-pass (wrong -- the whole point is
ranking the near-misses too) or a special-cased evaluate_candidate() that
knows about digest mode (a different kind of duplication -- coupling two
unrelated call shapes into one function). See the skill's "when NOT to
extract" section: the two orchestrations are genuinely different consumers
of the same lower-layer math, not duplicate copies of the same logic.

Unlike poll_route()/evaluate_candidate(), this module never touches dedup
or state.record_alert() -- a digest is a full snapshot every time, not an
incremental "what's new" feed, so there is nothing to dedup against. It DOES
read/write the SAME cash-baseline cache (state.get_baseline/update_baseline
via get_or_refresh_baseline) the real-time path uses, since that cache's
whole purpose -- bounding SerpApi call volume via ISO-week bucketing -- helps
the digest exactly the same way it helps the real-time triggers.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass

from src.cash import confirm_exact_date_price, get_or_refresh_baseline
from src.config import RouteConfig, WatchlistConfig
from src.providers.cash.base import CashFareProvider
from src.providers.seats_aero import AwardAvailability, SeatsAeroClient, SeatsAeroRateLimitError
from src.state import StateStore
from src.valuation import compute_effective_cpp, is_high_value, passes_award_prefilter

logger = logging.getLogger("digest")

# Two independent rankings, five each -- not one blended top-10 list. See
# deal-valuation's digest spec.
TOP_N = 5


@dataclass(frozen=True)
class DigestEntry:
    """One candidate's ranking-relevant numbers, snapshotted at digest-build
    time. `confirmed` is True only for the <=10 finalists (the union of both
    top-5 lists) that received a real exact-date cash confirm call -- every
    other entry's numbers come from the cheap, already-cached weekly-bucketed
    estimate (zero incremental SerpApi cost beyond what the real-time path's
    own baseline cache already bounds).

    `cleared_real_time_bar` is only ever set (non-None) on a confirmed entry
    -- it reflects is_high_value()'s real verdict against the CONFIRMED
    price/taxes, i.e. "would this have fired a real-time alert", reusing the
    exact same gate function rather than re-deriving the condition here.
    `real_time_cpp_floor` is captured for every entry (confirmed or not) so
    the notifier can report "closest was X.Xcpp, floor is Y.Ycpp" even when
    nothing in the digest ever reached the confirm stage."""

    award: AwardAvailability
    route: RouteConfig
    comparable_cash_usd: float
    taxes_usd: float
    cpp: float
    trip_value_usd: float
    real_time_cpp_floor: float
    confirmed: bool = False
    cleared_real_time_bar: bool | None = None


@dataclass(frozen=True)
class DigestResult:
    """Two independent rankings plus enough context for the "always sends,
    even honestly empty" case -- see deal-valuation's digest spec. Both lists
    are simultaneously empty or simultaneously non-empty (they're both
    top-N slices of the SAME `all_entries` list, just sorted differently),
    so notifiers only need to special-case the empty case once."""

    cash_rank: list[DigestEntry]
    cpp_rank: list[DigestEntry]
    candidates_evaluated: int   # every Cached Search hit seen, across all active routes
    candidates_ranked: int      # survivors of prefilter + known taxes + a resolved cash price


def _passes_ranking_prefilter(award: AwardAvailability, route: RouteConfig, config: WatchlistConfig) -> bool:
    """Same eligible_programs/cabin gate the real-time path applies before
    ever spending a cash lookup (src/valuation.py's passes_award_prefilter)
    -- no reason to rank a program the owner can't actually book through."""
    return passes_award_prefilter(award, set(route.cabins), config.eligible_programs)


def _rank_one_route(
    seats_client: SeatsAeroClient,
    origin: str,
    route: RouteConfig,
    config: WatchlistConfig,
    state: StateStore,
    cash_provider: CashFareProvider,
) -> tuple[list[DigestEntry], int, bool]:
    """Returns (entries, candidates_evaluated_this_origin, rate_limited).
    `rate_limited` signals the caller to stop querying seats.aero entirely
    for the rest of this digest build -- the daily quota has no per-minute
    reset, so there's nothing to gain from trying another origin/route this
    run (see CLAUDE.md's rate-limit guidance)."""
    start, end = route.date_window.to_dates()
    entries: list[DigestEntry] = []
    candidates_evaluated = 0

    try:
        hits = seats_client.cached_search(origin, route.destinations, start, end, route.cabins)
    except SeatsAeroRateLimitError as exc:
        logger.warning("rate limited on %s from %s during digest build, stopping: %s", route.name, origin, exc)
        return entries, candidates_evaluated, True

    for award in hits:
        candidates_evaluated += 1

        if not _passes_ranking_prefilter(award, route, config):
            continue

        # Unknown taxes (Qatar/Turkish/Singapore-style) mean a trustworthy
        # CPP can't be computed -- same rule as src/valuation.py's
        # is_high_value, never substitute 0.0.
        if award.taxes_usd is None:
            continue

        try:
            cash_update = get_or_refresh_baseline(
                state, cash_provider, origin=award.origin, destination=award.destination, cabin=award.cabin,
                date=award.date, max_age_minutes=config.schedule.cash_baseline_minutes,
            )
        except Exception:
            logger.warning(
                "cash baseline lookup failed for %s->%s (%s) on %s during digest build, excluding from ranking",
                award.origin, award.destination, award.cabin, award.date, exc_info=True,
            )
            continue

        comparable_cash_usd = None
        if cash_update.current_fare is not None:
            comparable_cash_usd = cash_update.current_fare.price_usd
        elif cash_update.baseline is not None:
            comparable_cash_usd = cash_update.baseline.ema_usd
        if comparable_cash_usd is None:
            continue

        cpp = compute_effective_cpp(comparable_cash_usd, award.taxes_usd, award.miles)
        trip_value_usd = comparable_cash_usd - award.taxes_usd
        entries.append(
            DigestEntry(
                award=award,
                route=route,
                comparable_cash_usd=comparable_cash_usd,
                taxes_usd=award.taxes_usd,
                cpp=cpp,
                trip_value_usd=trip_value_usd,
                real_time_cpp_floor=config.awards.cpp_floor(award.program),
            )
        )

    return entries, candidates_evaluated, False


def _confirm_finalists(
    finalists: list[DigestEntry],
    config: WatchlistConfig,
    cash_provider: CashFareProvider,
) -> dict[str, DigestEntry]:
    """Spends exactly one real exact-date confirm call per DISTINCT finalist
    (keyed by availability_id) -- a candidate present in both the cash-rank
    and cpp-rank top-5 lists is only confirmed once. Mirrors the real-time
    path's own confirm step (src/cash.py's confirm_exact_date_price) but does
    NOT call Get Trips: the digest reports the cheap Cached-Search taxes it
    already has (no incremental seats.aero quota spent), only re-pricing the
    cash side, which is the one number day-of-week variance actually makes
    unreliable at the weekly-bucket level (see deal-valuation)."""
    confirmed: dict[str, DigestEntry] = {}
    for entry in finalists:
        availability_id = entry.award.availability_id
        if availability_id in confirmed:
            continue

        try:
            confirmed_cash_usd = confirm_exact_date_price(
                cash_provider, origin=entry.award.origin, destination=entry.award.destination,
                cabin=entry.award.cabin, date=entry.award.date,
            )
        except Exception:
            logger.warning(
                "exact-date confirm failed for digest finalist %s, reporting the weekly estimate instead",
                availability_id, exc_info=True,
            )
            confirmed[availability_id] = entry
            continue

        if confirmed_cash_usd is None:
            confirmed[availability_id] = entry
            continue

        cpp = compute_effective_cpp(confirmed_cash_usd, entry.taxes_usd, entry.award.miles)
        trip_value_usd = confirmed_cash_usd - entry.taxes_usd
        verdict = is_high_value(
            entry.award, config.awards, set(entry.route.cabins),
            comparable_cash_usd=confirmed_cash_usd, taxes_usd=entry.taxes_usd,
        )
        confirmed[availability_id] = dataclasses.replace(
            entry,
            comparable_cash_usd=confirmed_cash_usd,
            cpp=cpp,
            trip_value_usd=trip_value_usd,
            confirmed=True,
            cleared_real_time_bar=verdict.fire,
        )

    return confirmed


def build_weekly_digest(
    config: WatchlistConfig,
    seats_client: SeatsAeroClient,
    cash_provider: CashFareProvider,
    state: StateStore,
) -> DigestResult:
    """Real Cached Search across every active route -> eligible_programs/
    cabin prefilter -> cheap weekly-bucketed CPP/trip-value ranking for every
    survivor (not just gate-passers) -> top-5-by-CPP + top-5-by-cash-value
    (independent lists, a candidate may appear in both) -> one real
    exact-date confirm call per distinct finalist -> DigestResult.

    Always returns a DigestResult, even when nothing survives ranking at all
    (both lists empty) -- callers (src/poller.py's run_digest(), notifiers)
    are responsible for turning that into an honest "no availability this
    week" message rather than treating it as an error. This is the actual
    fix for the real-time triggers' silence being indistinguishable from a
    broken pipeline -- see deal-valuation.
    """
    all_entries: list[DigestEntry] = []
    candidates_evaluated = 0

    for route in config.active_routes():
        origins = route.origins if route.origins is not None else config.origins
        rate_limited = False
        for origin in origins:
            entries, evaluated, rate_limited = _rank_one_route(
                seats_client, origin, route, config, state, cash_provider,
            )
            all_entries.extend(entries)
            candidates_evaluated += evaluated
            if rate_limited:
                break
        if rate_limited:
            break

    candidates_ranked = len(all_entries)

    cash_rank = sorted(all_entries, key=lambda e: e.trip_value_usd, reverse=True)[:TOP_N]
    cpp_rank = sorted(all_entries, key=lambda e: e.cpp, reverse=True)[:TOP_N]

    # Union of both lists, deduped by availability_id -- a candidate in both
    # top-5s only spends one confirm call, not two.
    finalists_by_id: dict[str, DigestEntry] = {}
    for entry in (*cash_rank, *cpp_rank):
        finalists_by_id.setdefault(entry.award.availability_id, entry)
    confirmed_by_id = _confirm_finalists(list(finalists_by_id.values()), config, cash_provider)

    def _finalize(rank_list: list[DigestEntry], key) -> list[DigestEntry]:
        finalized = [confirmed_by_id.get(e.award.availability_id, e) for e in rank_list]
        return sorted(finalized, key=key, reverse=True)

    return DigestResult(
        cash_rank=_finalize(cash_rank, lambda e: e.trip_value_usd),
        cpp_rank=_finalize(cpp_rank, lambda e: e.cpp),
        candidates_evaluated=candidates_evaluated,
        candidates_ranked=candidates_ranked,
    )
