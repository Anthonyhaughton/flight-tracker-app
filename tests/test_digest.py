"""Weekly digest tests -- all mocked, no real HTTP/creds.

Covers: independent top-5 ranking (cash-value vs CPP), the always-sends
behavior when nothing (or nothing eligible) is found, and the two-stage
cash-confirm step spending exactly one real call per DISTINCT finalist
across both top-5 lists (never per-list, never per-candidate-seen)."""

from __future__ import annotations

import datetime

from src.config import (
    AlertConfig,
    AwardConfig,
    CashConfig,
    DateWindow,
    RouteConfig,
    ScheduleConfig,
    WatchlistConfig,
)
from src.digest import build_weekly_digest
from src.providers.cash.base import CashFare
from src.providers.seats_aero import AwardAvailability
from src.state import InMemoryStateStore


class FakeSeatsAeroClient:
    def __init__(self, hits: list[AwardAvailability]):
        self._hits = hits
        self.get_trips_calls: list[str] = []

    def cached_search(self, origin, destinations, start, end, cabins) -> list[AwardAvailability]:
        return [h for h in self._hits if h.origin == origin and h.destination in destinations and h.cabin in cabins]

    def get_trips(self, availability_id: str):
        # The digest deliberately never spends a Get Trips call (see
        # src/digest.py's module docstring) -- recorded here so a test can
        # assert that invariant rather than just trusting the docstring.
        self.get_trips_calls.append(availability_id)
        return None

    def close(self) -> None:
        pass


class FakeCashFareProvider:
    """Returns `price_usd` for EVERY call, regardless of order or args --
    keeps the ranking math (weekly estimate) and the confirm-stage math
    (exact-date) identical and predictable across a test, so assertions can
    focus purely on CALL COUNT (how many times the provider was hit, and for
    which distinct finalists) rather than juggling per-call price sequencing."""

    def __init__(self, price_usd: float):
        self._price_usd = price_usd
        self.calls: list[tuple] = []

    def search(self, origin, destinations, start, end, cabin) -> list[CashFare]:
        self.calls.append((origin, destinations, start, end, cabin))
        return [
            CashFare(
                origin=origin, destination=destinations[0], date=start, return_date=None, cabin=cabin,
                price_usd=self._price_usd, airline="United", stops=0, deep_link=None,
            )
        ]

    def close(self) -> None:
        pass


def make_config(**overrides) -> WatchlistConfig:
    defaults = dict(
        origins=["IAD"],
        routes=[
            RouteConfig(
                name="DC → Italy",
                destinations=["FCO"],
                cabins=["economy"],
                date_window=DateWindow(start_offset=30, end_offset=330),
                active=True,
            ),
        ],
        awards=AwardConfig(min_trip_value_usd=500, cpp_floors={"default": 3.0}),
        cash=CashConfig(min_drop_pct=0.2, min_drop_abs_usd=150, mistake_fare_pct=0.45),
        schedule=ScheduleConfig(award_cached_minutes=20, cash_baseline_minutes=60),
        alerts=AlertConfig(dedup_ttl_days=5),
    )
    defaults.update(overrides)
    return WatchlistConfig(**defaults)


def make_award(**overrides) -> AwardAvailability:
    # All within ISO week 2026-W20 by default, so awards sharing this
    # date range share ONE cash-baseline bucket (see src/state.py's
    # baseline_key) -- only the first hit in a run actually costs a real
    # weekly-baseline provider call; the rest are served from cache, exactly
    # mirroring production's cost-bounding behavior.
    defaults = dict(
        origin="IAD",
        destination="FCO",
        date=datetime.date(2026, 5, 14),
        program="aeroplan",
        cabin="economy",
        miles=88000,
        taxes_usd=180.0,
        airlines=["AC"],
        direct=True,
        seats=2,
        availability_id="aeroplan-iad-fco-2026-05-14",
    )
    defaults.update(overrides)
    return AwardAvailability(**defaults)


def test_digest_always_sends_when_no_candidates_at_all():
    config = make_config()
    seats_client = FakeSeatsAeroClient([])
    cash_provider = FakeCashFareProvider(2000.0)
    state = InMemoryStateStore()

    result = build_weekly_digest(config, seats_client, cash_provider, state)

    assert result.cash_rank == []
    assert result.cpp_rank == []
    assert result.candidates_evaluated == 0
    assert result.candidates_ranked == 0
    assert cash_provider.calls == []  # nothing to rank -- never touches the cash provider


def test_digest_always_sends_when_everything_is_filtered_out():
    """Candidates exist and are seen (candidates_evaluated > 0), but none
    survive the eligible_programs prefilter or the unknown-taxes rule -- the
    digest must still return a valid (empty-ranked) result, not raise or
    silently drop the "we saw things but ranked nothing" signal. (A
    cabin-mismatch case isn't included here: real Cached Search -- and this
    file's FakeSeatsAeroClient, matching it -- is already requested scoped
    to route.cabins, so a wrong-cabin hit is filtered before the digest ever
    sees it, same as production; see src/poller.py's own note on this.)"""
    config = make_config(eligible_programs=["united"])  # excludes "aeroplan"
    awards = [
        make_award(program="aeroplan"),  # ineligible program
        make_award(program="united", taxes_usd=None, availability_id="united-unknown-taxes"),  # unknown taxes
    ]
    seats_client = FakeSeatsAeroClient(awards)
    cash_provider = FakeCashFareProvider(2000.0)
    state = InMemoryStateStore()

    result = build_weekly_digest(config, seats_client, cash_provider, state)

    assert result.cash_rank == []
    assert result.cpp_rank == []
    assert result.candidates_evaluated == 2
    assert result.candidates_ranked == 0
    assert cash_provider.calls == []  # every candidate was rejected before a cash lookup was ever spent


def test_digest_ranks_independently_and_confirms_only_distinct_union_of_finalists():
    """7 candidates, same cash baseline ($2,000, shared bucket -> ONE
    ranking-phase provider call), distinct (taxes, miles) chosen so the
    cash-value ranking and the CPP ranking are DIFFERENT-but-overlapping
    top-5 sets:

      trip_value = 2000 - taxes            cpp = trip_value / miles * 100
      A: taxes=100  miles=100000 -> 1900,  1.90
      B: taxes=150  miles=20000  -> 1850,  9.25
      C: taxes=200  miles=30000  -> 1800,  6.00
      D: taxes=250  miles=40000  -> 1750,  4.375
      E: taxes=300  miles=50000  -> 1700,  3.40
      F: taxes=1900 miles=5000   -> 100,   2.00
      G: taxes=1990 miles=1000   -> 10,    1.00

    cash_rank (top-5 by trip_value) = A, B, C, D, E -- excludes F, G.
    cpp_rank  (top-5 by cpp)        = B, C, D, E, F -- excludes A, G.
    Union of finalists = {A, B, C, D, E, F} = 6 distinct candidates (B-E
    overlap, A and F are each unique to one list, G is in neither).

    With cpp_floor=3.0 / min_trip_value_usd=500 (make_config's defaults):
    B, C, D, E clear the real-time bar; A (cpp too low) and F (trip value
    too low) do not -- a deliberate mix so cleared_real_time_bar is provably
    computed per-candidate, not just copied from list membership.

    Call-count assertion: 1 ranking-phase call (shared bucket) + 6 distinct
    finalist confirms = 7 total. A version that confirmed EACH list's 5
    independently (no dedup on the B-E overlap) would produce 1 + 10 = 11 --
    this test fails loudly on that regression."""
    awards = {
        "A": make_award(availability_id="A", date=datetime.date(2026, 5, 11), taxes_usd=100.0, miles=100000),
        "B": make_award(availability_id="B", date=datetime.date(2026, 5, 12), taxes_usd=150.0, miles=20000),
        "C": make_award(availability_id="C", date=datetime.date(2026, 5, 13), taxes_usd=200.0, miles=30000),
        "D": make_award(availability_id="D", date=datetime.date(2026, 5, 14), taxes_usd=250.0, miles=40000),
        "E": make_award(availability_id="E", date=datetime.date(2026, 5, 15), taxes_usd=300.0, miles=50000),
        "F": make_award(availability_id="F", date=datetime.date(2026, 5, 16), taxes_usd=1900.0, miles=5000),
        "G": make_award(availability_id="G", date=datetime.date(2026, 5, 17), taxes_usd=1990.0, miles=1000),
    }
    config = make_config()
    seats_client = FakeSeatsAeroClient(list(awards.values()))
    cash_provider = FakeCashFareProvider(2000.0)
    state = InMemoryStateStore()

    result = build_weekly_digest(config, seats_client, cash_provider, state)

    assert result.candidates_evaluated == 7
    assert result.candidates_ranked == 7

    cash_ids = [e.award.availability_id for e in result.cash_rank]
    cpp_ids = [e.award.availability_id for e in result.cpp_rank]
    assert cash_ids == ["A", "B", "C", "D", "E"]  # descending trip value
    assert cpp_ids == ["B", "C", "D", "E", "F"]   # descending cpp
    assert "G" not in cash_ids and "G" not in cpp_ids  # never a top-5 finalist in either list

    assert len(cash_provider.calls) == 7  # 1 ranking-phase + 6 distinct finalist confirms, not 11

    by_id = {e.award.availability_id: e for e in (*result.cash_rank, *result.cpp_rank)}
    assert all(e.confirmed for e in by_id.values())  # every finalist got a real exact-date confirm
    assert by_id["B"].cleared_real_time_bar is True
    assert by_id["C"].cleared_real_time_bar is True
    assert by_id["D"].cleared_real_time_bar is True
    assert by_id["E"].cleared_real_time_bar is True
    assert by_id["A"].cleared_real_time_bar is False  # cpp 1.9 < floor 3.0
    assert by_id["F"].cleared_real_time_bar is False  # trip value 100 < floor 500

    assert seats_client.get_trips_calls == []  # digest never spends a Get Trips call


def test_digest_selects_top_5_only_even_with_more_ranked_candidates():
    awards = [
        make_award(
            availability_id=f"award-{i}", date=datetime.date(2026, 5, 11 + i), taxes_usd=100.0 + i * 10,
            miles=50000,
        )
        for i in range(8)  # 8 distinct candidates, all sharing one cash baseline bucket
    ]
    config = make_config()
    seats_client = FakeSeatsAeroClient(awards)
    cash_provider = FakeCashFareProvider(2000.0)
    state = InMemoryStateStore()

    result = build_weekly_digest(config, seats_client, cash_provider, state)

    assert result.candidates_ranked == 8
    assert len(result.cash_rank) == 5
    assert len(result.cpp_rank) == 5
    # Lower taxes -> higher trip value AND higher cpp here (miles constant),
    # so both rankings pick the same 5 lowest-tax awards, just for clarity
    # confirm they're sorted descending by their own metric.
    assert [e.trip_value_usd for e in result.cash_rank] == sorted(
        (e.trip_value_usd for e in result.cash_rank), reverse=True
    )
    assert [e.cpp for e in result.cpp_rank] == sorted((e.cpp for e in result.cpp_rank), reverse=True)


def test_digest_excludes_candidate_with_unknown_taxes_from_ranking():
    """Unknown taxes (taxes_usd=None, e.g. qatar/turkish/singapore) must
    never be treated as $0 for ranking purposes either -- same rule as
    src/valuation.py's is_high_value, applied here so the digest can't
    silently inflate a program's CPP into the rankings."""
    config = make_config()
    award = make_award(taxes_usd=None)
    seats_client = FakeSeatsAeroClient([award])
    cash_provider = FakeCashFareProvider(2000.0)
    state = InMemoryStateStore()

    result = build_weekly_digest(config, seats_client, cash_provider, state)

    assert result.candidates_evaluated == 1
    assert result.candidates_ranked == 0
    assert result.cash_rank == []
    assert result.cpp_rank == []
    assert cash_provider.calls == []  # rejected before a cash lookup was ever spent
