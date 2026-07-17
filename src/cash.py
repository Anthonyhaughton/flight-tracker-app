"""Baseline caching: decides whether a cached cash-price baseline is fresh
enough to reuse, or whether it's time to spend a real CashFareProvider call
to refresh it. This is what bounds SerpApi call volume -- see
.claude/skills/flight-cash-price-monitor's "cache aggressively" guidance and
watchlist.yaml's schedule.cash_baseline_minutes.

One cached baseline (keyed by route+cabin+date-bucket, see
state.baseline_key) serves both v1.1 use cases, wired together in
src/poller.py: the award valuation gate's cash comparison, and the
standalone cash-price-drop trigger.

Provider errors (auth, rate limit, network) are allowed to propagate --
per .claude/skills/flight-cash-price-monitor, "a provider hiccup should log
and skip, not crash the whole poll run," but deciding how to degrade
gracefully belongs to the caller (poller.py already has that pattern for
seats.aero), not this orchestration layer.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from src.providers.cash.base import CashFare, CashFareProvider
from src.state import Baseline, StateStore, baseline_key


@dataclass(frozen=True)
class CashBaselineUpdate:
    key: str
    baseline: Baseline | None       # current baseline after this call (fresh read or freshly refreshed)
    previous: Baseline | None       # baseline as it stood BEFORE this call -- None only on a first-ever seed
    current_fare: CashFare | None   # the real fare just observed, only set when refreshed and a fare was found
    refreshed: bool                 # True if this call actually hit the provider (cache was missing/stale)


def get_or_refresh_baseline(
    state: StateStore,
    provider: CashFareProvider,
    *,
    origin: str,
    destination: str,
    cabin: str,
    date: datetime.date,
    max_age_minutes: int,
) -> CashBaselineUpdate:
    key = baseline_key(origin, destination, cabin, date)
    existing = state.get_baseline(key)

    if existing is not None:
        age = datetime.datetime.now(datetime.timezone.utc) - existing.updated_at
        if age < datetime.timedelta(minutes=max_age_minutes):
            return CashBaselineUpdate(key=key, baseline=existing, previous=existing, current_fare=None, refreshed=False)

    fares = provider.search(origin, [destination], date, date, cabin)
    if not fares:
        # Genuinely no fare found, or a provider-level hiccup the provider
        # itself already logged and swallowed (see CashFareProvider impls) --
        # either way, keep serving the existing (possibly stale, possibly
        # None) baseline rather than corrupt it with a missing observation.
        return CashBaselineUpdate(key=key, baseline=existing, previous=existing, current_fare=None, refreshed=False)

    cheapest = min(fares, key=lambda f: f.price_usd)
    state.update_baseline(key, cheapest.price_usd)
    refreshed_baseline = state.get_baseline(key)

    return CashBaselineUpdate(key=key, baseline=refreshed_baseline, previous=existing, current_fare=cheapest, refreshed=True)


def confirm_exact_date_price(
    provider: CashFareProvider,
    *,
    origin: str,
    destination: str,
    cabin: str,
    date: datetime.date,
) -> float | None:
    """Bypasses the baseline cache entirely -- always a real provider call
    for this candidate's EXACT date, not the week-bucketed proxy
    get_or_refresh_baseline returns.

    Used as a final-confirm step (see src/poller.py) for a candidate that
    already cleared every other filter (prefilter, weekly-bucketed CPP gate,
    dedup, cap, Get Trips), mirroring seats.aero's Cached-Search-then-
    Get-Trips pattern: cheap/bucketed data decides who's a candidate, one
    precise call per finalist confirms the real number before ever sending.
    Day-of-week price variance on long-haul business fares can be large
    enough that the week's bucketed baseline isn't trustworthy as the
    number that actually gates a real alert.

    Returns None (never the bucketed value) if the provider finds nothing --
    callers must treat that as "can't confirm," not "assume it's fine,"
    same as an unknown tax is never treated as free.
    """
    fares = provider.search(origin, [destination], date, date, cabin)
    if not fares:
        return None
    return min(f.price_usd for f in fares)
