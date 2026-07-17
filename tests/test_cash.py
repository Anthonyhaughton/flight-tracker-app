from __future__ import annotations

import datetime

from src.cash import confirm_exact_date_price, get_or_refresh_baseline
from src.providers.cash.base import CashFare
from src.state import Baseline, InMemoryStateStore, baseline_key


class FakeCashFareProvider:
    """Records every search() call so tests can assert exactly how many
    times (if any) the provider was actually hit -- that call count is the
    whole point of baseline caching."""

    def __init__(self, fares_by_call: list[list[CashFare]]):
        self.calls: list[tuple] = []
        self._fares_by_call = fares_by_call

    def search(self, origin, destinations, start, end, cabin) -> list[CashFare]:
        self.calls.append((origin, destinations, start, end, cabin))
        idx = len(self.calls) - 1
        return self._fares_by_call[idx] if idx < len(self._fares_by_call) else []

    def close(self) -> None:
        pass


def make_fare(price_usd: float, **overrides) -> CashFare:
    defaults = dict(
        origin="IAD",
        destination="FCO",
        date=datetime.date(2026, 9, 14),
        return_date=None,
        cabin="business",
        price_usd=price_usd,
        airline="United",
        stops=0,
        deep_link=None,
    )
    defaults.update(overrides)
    return CashFare(**defaults)


def test_first_lookup_seeds_baseline_and_hits_provider():
    provider = FakeCashFareProvider([[make_fare(5900.0)]])
    state = InMemoryStateStore()

    update = get_or_refresh_baseline(
        state, provider, origin="IAD", destination="FCO", cabin="business",
        date=datetime.date(2026, 9, 14), max_age_minutes=60,
    )

    assert len(provider.calls) == 1
    assert update.refreshed is True
    assert update.previous is None  # first-ever observation -- caller must not alert on this
    assert update.current_fare.price_usd == 5900.0
    assert update.baseline.trailing_min_usd == 5900.0
    assert update.baseline.ema_usd == 5900.0


def test_second_lookup_within_refresh_window_reuses_cache():
    """This is what bounds SerpApi call volume: a second candidate for the
    same route/cabin/date-bucket within cash_baseline_minutes must NOT
    trigger a second real provider call."""
    provider = FakeCashFareProvider([[make_fare(5900.0)], [make_fare(4000.0)]])
    state = InMemoryStateStore()

    get_or_refresh_baseline(
        state, provider, origin="IAD", destination="FCO", cabin="business",
        date=datetime.date(2026, 9, 14), max_age_minutes=60,
    )
    update2 = get_or_refresh_baseline(
        state, provider, origin="IAD", destination="FCO", cabin="business",
        date=datetime.date(2026, 9, 15), max_age_minutes=60,  # same ISO week -> same bucket
    )

    assert len(provider.calls) == 1  # NOT called a second time
    assert update2.refreshed is False
    assert update2.current_fare is None
    assert update2.baseline.trailing_min_usd == 5900.0  # still the first observation, not the second fare


def test_lookup_after_window_expires_refetches():
    provider = FakeCashFareProvider([[make_fare(5900.0)], [make_fare(4000.0)]])
    state = InMemoryStateStore()
    date = datetime.date(2026, 9, 14)

    get_or_refresh_baseline(
        state, provider, origin="IAD", destination="FCO", cabin="business", date=date, max_age_minutes=60,
    )
    # Directly age the cached baseline past the refresh window rather than
    # mocking datetime.now() globally -- simplest way to simulate "an hour
    # has passed" deterministically.
    key = baseline_key("IAD", "FCO", "business", date)
    stale = state._baselines[key]
    state._baselines[key] = Baseline(
        trailing_min_usd=stale.trailing_min_usd,
        ema_usd=stale.ema_usd,
        updated_at=stale.updated_at - datetime.timedelta(minutes=61),
    )

    update2 = get_or_refresh_baseline(
        state, provider, origin="IAD", destination="FCO", cabin="business", date=date, max_age_minutes=60,
    )

    assert len(provider.calls) == 2
    assert update2.refreshed is True
    assert update2.current_fare.price_usd == 4000.0
    assert update2.previous.trailing_min_usd == 5900.0  # the pre-refresh baseline, for drop comparison
    assert update2.baseline.trailing_min_usd == 4000.0  # min(5900, 4000)


def test_lookup_keeps_existing_baseline_when_provider_returns_nothing():
    provider = FakeCashFareProvider([[make_fare(5900.0)], []])  # second call finds nothing
    state = InMemoryStateStore()
    date = datetime.date(2026, 9, 14)

    get_or_refresh_baseline(
        state, provider, origin="IAD", destination="FCO", cabin="business", date=date, max_age_minutes=60,
    )
    key = baseline_key("IAD", "FCO", "business", date)
    stale = state._baselines[key]
    state._baselines[key] = Baseline(
        trailing_min_usd=stale.trailing_min_usd, ema_usd=stale.ema_usd,
        updated_at=stale.updated_at - datetime.timedelta(minutes=61),
    )

    update2 = get_or_refresh_baseline(
        state, provider, origin="IAD", destination="FCO", cabin="business", date=date, max_age_minutes=60,
    )

    assert len(provider.calls) == 2  # it DID try
    assert update2.refreshed is False  # but found nothing, so no baseline corruption
    assert update2.current_fare is None
    assert update2.baseline.trailing_min_usd == 5900.0  # unchanged


def test_different_dates_in_same_iso_week_share_one_baseline_bucket():
    provider = FakeCashFareProvider([[make_fare(5900.0)]])
    state = InMemoryStateStore()

    # 2026-09-14 (Mon) and 2026-09-16 (Wed) are the same ISO week.
    get_or_refresh_baseline(
        state, provider, origin="IAD", destination="FCO", cabin="business",
        date=datetime.date(2026, 9, 14), max_age_minutes=60,
    )
    update2 = get_or_refresh_baseline(
        state, provider, origin="IAD", destination="FCO", cabin="business",
        date=datetime.date(2026, 9, 16), max_age_minutes=60,
    )

    assert len(provider.calls) == 1
    assert update2.refreshed is False


# --- confirm_exact_date_price: the final-confirm step, bypasses caching entirely ---


def test_confirm_exact_date_price_returns_cheapest_fare():
    provider = FakeCashFareProvider([[make_fare(5900.0), make_fare(6200.0)]])
    price = confirm_exact_date_price(
        provider, origin="IAD", destination="FCO", cabin="business", date=datetime.date(2026, 9, 14)
    )
    assert price == 5900.0


def test_confirm_exact_date_price_returns_none_when_nothing_found():
    provider = FakeCashFareProvider([[]])
    price = confirm_exact_date_price(
        provider, origin="IAD", destination="FCO", cabin="business", date=datetime.date(2026, 9, 14)
    )
    assert price is None


def test_confirm_exact_date_price_never_reads_or_writes_the_cached_baseline():
    """Unlike get_or_refresh_baseline, this always spends a real call and
    never consults or updates state -- it takes a provider directly, not a
    StateStore, so there's nothing for it to read/write in the first
    place. Calling it twice must hit the provider twice, with no caching."""
    provider = FakeCashFareProvider([[make_fare(5900.0)], [make_fare(4000.0)]])
    date = datetime.date(2026, 9, 14)

    first = confirm_exact_date_price(provider, origin="IAD", destination="FCO", cabin="business", date=date)
    second = confirm_exact_date_price(provider, origin="IAD", destination="FCO", cabin="business", date=date)

    assert first == 5900.0
    assert second == 4000.0
    assert len(provider.calls) == 2
