"""End-to-end poller test with fake providers -- no real HTTP, no real creds."""

from __future__ import annotations

import dataclasses
import datetime
import logging

from src.config import AwardConfig, CashConfig, AlertConfig, DateWindow, RouteConfig, ScheduleConfig, WatchlistConfig
from src.poller import run
from src.providers.cash.base import CashFare
from src.providers.seats_aero import AwardAvailability
from src.state import InMemoryStateStore, award_key, baseline_key, cash_key


class FakeSeatsAeroClient:
    def __init__(self, hits: list[AwardAvailability]):
        self._hits = hits
        self.get_trips_calls: list[str] = []
        self.cached_search_origins: list[str] = []

    def cached_search(self, origin, destinations, start, end, cabins) -> list[AwardAvailability]:
        self.cached_search_origins.append(origin)
        return [h for h in self._hits if h.origin == origin and h.destination in destinations and h.cabin in cabins]

    def get_trips(self, availability_id: str):
        self.get_trips_calls.append(availability_id)
        for hit in self._hits:
            if hit.availability_id == availability_id:
                # Mirrors the real API: Get Trips returns itineraries across
                # ALL cabins for the availability, with a decoy (wrong-cabin,
                # deliberately cheaper, listed FIRST) trip -- so any test
                # using this fake would catch a regression back to trips[0].
                decoy_cabin = "economy" if hit.cabin != "economy" else "premium_economy"
                return [
                    {
                        "ID": f"{availability_id}-decoy",
                        "AvailabilityID": availability_id,
                        "MileageCost": 1,
                        "TotalTaxes": 100,
                        "Cabin": decoy_cabin,
                        "RemainingSeats": 9,
                        "FlightNumbers": "XX000",
                    },
                    {
                        "ID": f"{availability_id}-trip1",
                        "AvailabilityID": availability_id,
                        "MileageCost": hit.miles,
                        "TotalTaxes": 18000,
                        "Cabin": hit.cabin,
                        "RemainingSeats": hit.seats,
                        "FlightNumbers": "AC942",
                    },
                ]
        return None

    def close(self) -> None:
        pass


class FakeCashFareProvider:
    """Defaults to finding nothing (empty search results) every call. Since
    the no-cash-data path always skips now (no cabin-match-alone fallback
    -- see src/valuation.py's is_high_value), that default means "every
    award skips" unless a test explicitly supplies data.

    Tests that care about exact cash specifics (prices, call counts) pass
    `fares_by_call` explicitly, indexed by call order. Tests that just need
    every award to reliably clear the cash gate, without caring about exact
    call counts or which weekly bucket/exact-date call is which (e.g. the
    cap/dedup tests), pass `default_fare` instead -- it's returned for
    every call beyond what `fares_by_call` explicitly covers, rather than
    falling through to an empty (now skip-causing) result."""

    def __init__(self, fares_by_call: list[list[CashFare]] | None = None, default_fare: CashFare | None = None):
        self.calls: list[tuple] = []
        self._fares_by_call = fares_by_call or []
        self._default_fare = default_fare

    def search(self, origin, destinations, start, end, cabin) -> list[CashFare]:
        self.calls.append((origin, destinations, start, end, cabin))
        idx = len(self.calls) - 1
        if idx < len(self._fares_by_call):
            return self._fares_by_call[idx]
        return [self._default_fare] if self._default_fare is not None else []

    def close(self) -> None:
        pass


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple] = []  # (award, verdict, trip, deep_link, transfer_bonus_pct, group_other_dates)
        self.cash_sent: list[tuple] = []  # (fare, verdict, baseline)
        self.closed = False

    def send_award_alert(
        self, award, verdict, trip, *, deep_link=None, transfer_bonus_pct=0.0, group_other_dates=None,
    ) -> None:
        self.sent.append((award, verdict, trip, deep_link, transfer_bonus_pct, group_other_dates))

    def send_cash_alert(self, fare, verdict, baseline) -> None:
        self.cash_sent.append((fare, verdict, baseline))

    def close(self) -> None:
        self.closed = True


class FakeHeartbeat:
    def __init__(self):
        self.emitted = 0

    def emit(self) -> None:
        self.emitted += 1


def make_config() -> WatchlistConfig:
    return WatchlistConfig(
        origins=["IAD"],
        routes=[
            RouteConfig(
                name="DC → Italy",
                destinations=["FCO"],
                cabins=["business", "first"],
                date_window=DateWindow(start_offset=30, end_offset=330),
                active=True,
            ),
            RouteConfig(
                name="inactive route",
                destinations=["LHR"],
                cabins=["economy"],
                date_window=DateWindow(start_offset=30, end_offset=330),
                active=False,
            ),
        ],
        awards=AwardConfig(min_trip_value_usd=1500, cpp_floors={"default": 1.4, "aeroplan": 1.5}),
        cash=CashConfig(min_drop_pct=0.2, min_drop_abs_usd=150, mistake_fare_pct=0.45),
        schedule=ScheduleConfig(award_cached_minutes=20, cash_baseline_minutes=60),
        alerts=AlertConfig(dedup_ttl_days=5),
    )


def make_award(**overrides) -> AwardAvailability:
    defaults = dict(
        origin="IAD",
        destination="FCO",
        date=datetime.date(2026, 5, 14),
        program="aeroplan",
        cabin="business",
        miles=88000,
        taxes_usd=180.0,
        airlines=["AC"],
        direct=True,
        seats=2,
        availability_id="aeroplan-iad-fco-2026-05-14",
        # economy_miles=50000 -> 88000/50000 = 1.76x, clears the default 2.0x
        # premium_cabin_max_multiplier -- keeps every pre-existing test that
        # doesn't care about the premium-cabin prefilter passing unchanged.
        # A test that specifically wants the ratio check to REJECT overrides
        # this explicitly (see the premium-cabin prefilter tests below).
        economy_miles=50000,
    )
    defaults.update(overrides)
    return AwardAvailability(**defaults)


def make_fare(**overrides) -> CashFare:
    defaults = dict(
        origin="IAD",
        destination="FCO",
        date=datetime.date(2026, 5, 14),
        return_date=None,
        cabin="business",
        price_usd=5900.0,
        airline="United",
        stops=0,
        deep_link=None,
    )
    defaults.update(overrides)
    return CashFare(**defaults)


def test_poller_passes_real_taxes_to_first_valuation_call(monkeypatch):
    """The initial gate must use Cached Search's real award.taxes_usd, not a
    0.0 placeholder -- spy on is_high_value to check what's actually passed
    the first time it's called, before Get Trips even runs."""
    import src.poller as poller_module
    from src.valuation import Verdict

    calls = []

    def spy_is_high_value(award, config, wanted_cabins, comparable_cash_usd=None, taxes_usd=None):
        calls.append(taxes_usd)
        return Verdict(True, "stub", "stub")

    monkeypatch.setattr(poller_module, "is_high_value", spy_is_high_value)

    config = make_config()
    award = make_award(taxes_usd=222.5)
    seats_client = FakeSeatsAeroClient([award])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    run(config, cash_provider=FakeCashFareProvider(), seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat)

    assert calls[0] == 222.5  # first call used the award's real taxes, not a placeholder


def test_poller_sends_alert_for_business_award():
    config = make_config()
    award = make_award()
    seats_client = FakeSeatsAeroClient([award])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(
        config, cash_provider=FakeCashFareProvider(default_fare=make_fare(price_usd=5900.0)),
        seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat,
    )

    assert alerts_sent == 1
    assert len(notifier.sent) == 1
    assert notifier.sent[0][0].origin == "IAD"
    assert seats_client.get_trips_calls == ["aeroplan-iad-fco-2026-05-14"]
    assert heartbeat.emitted == 1


def test_poller_selects_matching_cabin_trip_not_first_trip():
    """Regression (found in the first live dry run): FakeSeatsAeroClient's
    get_trips() returns a decoy wrong-cabin trip first (mirroring the real
    API), so a naive trips[0] would hand the notifier the decoy's 1 mile /
    economy cabin instead of the real business award."""
    config = make_config()
    award = make_award()
    seats_client = FakeSeatsAeroClient([award])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    run(
        config, cash_provider=FakeCashFareProvider(default_fare=make_fare(price_usd=5900.0)),
        seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat,
    )

    sent_trip = notifier.sent[0][2]
    assert sent_trip["Cabin"] == "business"
    assert sent_trip["MileageCost"] == award.miles


def test_poller_skips_untracked_cabin():
    config = make_config()
    award = make_award(cabin="economy")
    seats_client = FakeSeatsAeroClient([award])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(config, cash_provider=FakeCashFareProvider(), seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat)

    assert alerts_sent == 0
    assert notifier.sent == []
    assert heartbeat.emitted == 1  # a clean run with zero deals still heartbeats


def test_poller_ignores_inactive_route():
    config = make_config()
    award = make_award(destination="LHR", cabin="economy")
    seats_client = FakeSeatsAeroClient([award])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(config, cash_provider=FakeCashFareProvider(), seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat)

    assert alerts_sent == 0


def test_poller_dedups_on_second_run_same_deal():
    config = make_config()
    award = make_award()
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    first_run = run(
        config, cash_provider=FakeCashFareProvider(default_fare=make_fare(price_usd=5900.0)),
        seats_client=FakeSeatsAeroClient([award]), state=state, notifier=notifier, heartbeat=heartbeat,
    )
    # Second run's provider is never actually called: the baseline seeded by
    # the first run is still fresh (same real time, cash_baseline_minutes
    # window), and this is the SAME award -- dedup catches it before either
    # a fresh baseline lookup or an exact-date confirm would be attempted.
    second_run = run(
        config, cash_provider=FakeCashFareProvider(), seats_client=FakeSeatsAeroClient([award]), state=state,
        notifier=notifier, heartbeat=heartbeat,
    )

    assert first_run == 1
    assert second_run == 0
    assert len(notifier.sent) == 1
    assert heartbeat.emitted == 2  # both runs completed cleanly


def test_poller_re_alerts_when_price_crosses_lower_bucket():
    config = make_config()
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    first = make_award(miles=88000)
    run(
        config, cash_provider=FakeCashFareProvider(default_fare=make_fare(price_usd=5900.0)),
        seats_client=FakeSeatsAeroClient([first]), state=state, notifier=notifier, heartbeat=heartbeat,
    )

    better = make_award(miles=60000, availability_id="aeroplan-iad-fco-2026-05-14-v2")
    # Same route/cabin/date -> same baseline_key -- the weekly lookup hits
    # cache (no call), but this is a DIFFERENT award_key (different miles
    # bucket), so it's not deduped and DOES reach the exact-date confirm,
    # which needs its own real fare.
    second_alerts = run(
        config, cash_provider=FakeCashFareProvider(default_fare=make_fare(price_usd=5900.0)),
        seats_client=FakeSeatsAeroClient([better]), state=state, notifier=notifier, heartbeat=heartbeat,
    )

    assert second_alerts == 1
    assert len(notifier.sent) == 2


def make_distinct_awards(n: int) -> list[AwardAvailability]:
    """n distinct qualifying awards -- different dates/availability_ids AND
    different miles buckets, so each gets its own dedup key (award_key
    buckets by miles // 5000 * 5000) and none collide with each other."""
    return [
        make_award(
            availability_id=f"aeroplan-iad-fco-2026-{5 + i:02d}-14",
            # A different CALENDAR MONTH per award, not just a different day
            # within May -- select_group_winners() groups by (origin,
            # destination, cabin, program, month), so same-month-different-
            # day awards would now collapse to a single winner, which is
            # exactly wrong for tests about the cap/dedup mechanism across
            # genuinely INDEPENDENT candidates (see .claude/skills/
            # deal-valuation's winner-selection spec).
            date=datetime.date(2026, 5 + i, 14),
            miles=88000 + i * 10000,
            # economy_miles scales alongside miles so the ratio stays well
            # under the default 2.0x premium_cabin_max_multiplier regardless
            # of n -- these awards are meant to differ only in date/miles-
            # bucket/dedup-key, not accidentally trip the premium-cabin
            # prefilter as n grows.
            economy_miles=50000 + i * 10000,
        )
        for i in range(n)
    ]


def test_run_enforces_max_alerts_per_run_cap():
    """Regression: a real production run alerted 73 times in one invoke
    because there was no per-run cap -- a wide date window + an empty dedup
    table let every qualifying candidate through. 5 distinct new qualifying
    awards (never seen before, so dedup can't be what's limiting them),
    cap=3 -- only 3 may send even though all 5 clear the valuation gate."""
    config = dataclasses.replace(make_config(), alerts=AlertConfig(dedup_ttl_days=5, max_alerts_per_run=3))
    awards = make_distinct_awards(5)
    seats_client = FakeSeatsAeroClient(awards)
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(
        config, cash_provider=FakeCashFareProvider(default_fare=make_fare(price_usd=5900.0)),
        seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat,
    )

    assert alerts_sent == 3
    assert len(notifier.sent) == 3


def test_poll_route_counts_duplicate_and_capped_skips_separately():
    """Dedup and the cap are independent gates (dedup = same deal on a LATER
    run; cap = too many NEW deals in ONE run) -- confirm PollStats keeps them
    in separate counters rather than lumping both into one 'skipped' bucket.
    First pass: 5 new awards, cap=3 -> 3 sent, 2 capped, 0 duplicate. Second
    pass with fresh stats but the SAME state: the 3 already-sent awards are
    now duplicates (not capped -- the cap counter resets per run and never
    even got exercised by them), and the other 2 still have cap budget."""
    from src.poller import poll_route

    config = make_config()
    route = config.routes[0]
    awards = make_distinct_awards(5)
    state = InMemoryStateStore()
    notifier = FakeNotifier()

    cash_provider = FakeCashFareProvider(default_fare=make_fare(price_usd=5900.0))
    stats = poll_route(
        FakeSeatsAeroClient(awards), config.origins, route, config, state, notifier,
        cash_provider=cash_provider, max_alerts_per_run=3,
    )
    assert stats.alerts_sent == 3
    assert stats.skipped_capped == 2
    assert stats.skipped_duplicate == 0

    stats2 = poll_route(
        FakeSeatsAeroClient(awards), config.origins, route, config, state, notifier,
        cash_provider=cash_provider, max_alerts_per_run=3,
    )
    assert stats2.skipped_duplicate == 3
    assert stats2.alerts_sent == 2
    assert stats2.skipped_capped == 0


def test_run_logs_cap_and_duplicate_counts_separately(caplog):
    """The end-of-run summary log line must make the cap legible as a
    distinct number from duplicates and from total evaluated/sent, so a
    future run's logs make it obvious whether the cap was the limiting
    factor (this is precisely what was invisible during the 73-alert
    flood)."""
    config = dataclasses.replace(make_config(), alerts=AlertConfig(dedup_ttl_days=5, max_alerts_per_run=1))
    awards = make_distinct_awards(3)
    seats_client = FakeSeatsAeroClient(awards)
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    with caplog.at_level(logging.INFO, logger="poller"):
        run(
            config, cash_provider=FakeCashFareProvider(default_fare=make_fare(price_usd=5900.0)),
            seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat,
        )

    summary = next(r.getMessage() for r in caplog.records if "poll complete" in r.getMessage())
    assert "3 candidate(s) evaluated" in summary
    assert "1 alert(s) sent" in summary
    assert "0 skipped as duplicate" in summary
    assert "2 skipped (cap reached)" in summary


# --- v1.1: real cash wiring into the CPP gate + the independent cash-drop trigger ---


def _force_baseline_stale(state, origin, destination, cabin, date):
    """Directly ages a cached baseline past any realistic max_age_minutes,
    rather than mocking datetime.now() globally -- the simplest deterministic
    way to simulate "the refresh window has elapsed" between two run() calls
    in a test."""
    key = baseline_key(origin, destination, cabin, date)
    stale = state._baselines[key]
    state._baselines[key] = dataclasses.replace(stale, updated_at=stale.updated_at - datetime.timedelta(minutes=999))


def test_poller_rejects_award_when_real_cash_comparison_fails_cpp_floor():
    """Contrast with v1.0's test_poller_sends_alert_for_business_award (no
    cash provider injected -> comparable_cash_usd stays None -> cabin match
    alone fires): once a real cash baseline is wired in and the comparison
    is unfavorable, the award must be REJECTED, not fire on cabin match
    alone. $1,000 cash vs 88,000 miles + $180 taxes clears neither the CPP
    floor nor min_trip_value_usd."""
    config = make_config()
    award = make_award()
    seats_client = FakeSeatsAeroClient([award])
    cash_provider = FakeCashFareProvider([[make_fare(price_usd=1000.0)]])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(
        config, cash_provider=cash_provider, seats_client=seats_client, state=state, notifier=notifier,
        heartbeat=heartbeat,
    )

    assert alerts_sent == 0
    assert notifier.sent == []


def test_poller_fires_award_when_real_cash_comparison_clears_floor():
    config = make_config()
    award = make_award()
    seats_client = FakeSeatsAeroClient([award])
    # (5900-180)/88000*100 = 6.5cpp, well above aeroplan's 1.5 floor.
    # Two calls queued: [0] the weekly-bucketed first-pass lookup, [1] the
    # exact-date confirm spent once the candidate clears every other filter.
    cash_provider = FakeCashFareProvider([[make_fare(price_usd=5900.0)], [make_fare(price_usd=5900.0)]])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(
        config, cash_provider=cash_provider, seats_client=seats_client, state=state, notifier=notifier,
        heartbeat=heartbeat,
    )

    assert alerts_sent == 1
    assert notifier.sent[0][0].origin == "IAD"
    assert len(cash_provider.calls) == 2


def test_poller_rejects_when_exact_date_confirm_fails_cpp_floor_after_weekly_check_passed():
    """Day-of-week price variance: the cached weekly-bucketed baseline can
    be favorable while the award's EXACT date is not. A candidate that
    clears the cheap/weekly CPP prefilter must still be rejected if the
    one-additional-call exact-date confirm comes back unfavorable -- the
    weekly bucket is not accurate enough to be the final number that gates
    a real alert."""
    config = make_config()
    award = make_award()
    seats_client = FakeSeatsAeroClient([award])
    # [0] weekly-bucket lookup: $5,900 -> (5900-180)/88000*100 = 6.5cpp, clears the floor.
    # [1] exact-date confirm: $1,000 -> (1000-180)/88000*100 = 0.93cpp, fails the floor.
    cash_provider = FakeCashFareProvider([[make_fare(price_usd=5900.0)], [make_fare(price_usd=1000.0)]])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(
        config, cash_provider=cash_provider, seats_client=seats_client, state=state, notifier=notifier,
        heartbeat=heartbeat,
    )

    assert alerts_sent == 0
    assert notifier.sent == []
    assert len(cash_provider.calls) == 2  # weekly-bucket lookup + exact-date confirm, nothing more
    assert state.already_alerted(award_key(award)) is False  # never recorded, since nothing was sent


def test_poller_sends_using_exact_date_confirmed_price_not_weekly_bucketed_price():
    """The confirm call's price -- not the weekly-bucketed one -- is what
    the final verdict/headline is actually built from."""
    config = make_config()
    award = make_award()
    seats_client = FakeSeatsAeroClient([award])
    # [0] weekly-bucket lookup: $1,700 -> barely clears (1.73cpp >= 1.5 floor, $1,520 trip value >= $1,500).
    # [1] exact-date confirm: $7,000 -> a much better real price.
    cash_provider = FakeCashFareProvider([[make_fare(price_usd=1700.0)], [make_fare(price_usd=7000.0)]])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(
        config, cash_provider=cash_provider, seats_client=seats_client, state=state, notifier=notifier,
        heartbeat=heartbeat,
    )

    assert alerts_sent == 1
    sent_verdict = notifier.sent[0][1]
    assert "$7,000" in sent_verdict.headline
    assert "$1,700" not in sent_verdict.headline


def test_poller_skips_when_no_cash_provider_data_at_all():
    """When there's no real cash data to begin with (e.g. an empty
    provider), the award must be SKIPPED, not fired on cabin-match-alone --
    an earlier version fell back to firing here (v1.0-style, "results are
    already saver-equivalent by construction"), but that fallback direction
    was retired as a real safety issue: a cash-pipeline outage's failure
    mode must never be MORE alerts. The exact-date confirm step correctly
    never runs either -- there's nothing to confirm, and the award is
    already rejected before reaching that point."""
    config = make_config()
    award = make_award()
    seats_client = FakeSeatsAeroClient([award])
    cash_provider = FakeCashFareProvider()  # always returns [] -- no cash data anywhere
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(
        config, cash_provider=cash_provider, seats_client=seats_client, state=state, notifier=notifier,
        heartbeat=heartbeat,
    )

    assert alerts_sent == 0
    assert notifier.sent == []
    # Only the weekly-bucket lookup call happens -- no second (confirm) call,
    # since the award never reaches Get Trips.
    assert len(cash_provider.calls) == 1
    assert seats_client.get_trips_calls == []


def test_poller_never_fires_cash_drop_on_first_observation():
    """Never alert before a baseline exists -- the first observation of a
    route seeds the baseline silently, per deal-valuation."""
    config = make_config()
    award = make_award()
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    run(
        config, cash_provider=FakeCashFareProvider([[make_fare(price_usd=6000.0)]]),
        seats_client=FakeSeatsAeroClient([award]), state=state, notifier=notifier, heartbeat=heartbeat,
    )

    assert notifier.cash_sent == []


def test_poller_fires_cash_drop_alert_once_baseline_exists_and_price_drops():
    config = make_config()
    award = make_award()
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    # Seed the baseline at $6,000 (first observation -- no alert).
    run(
        config, cash_provider=FakeCashFareProvider([[make_fare(price_usd=6000.0)]]),
        seats_client=FakeSeatsAeroClient([award]), state=state, notifier=notifier, heartbeat=heartbeat,
    )
    _force_baseline_stale(state, "IAD", "FCO", "business", award.date)

    # $4,000 vs the $6,000 baseline = 33% drop, clears min_drop_pct (20%).
    run(
        config, cash_provider=FakeCashFareProvider([[make_fare(price_usd=4000.0)]]),
        seats_client=FakeSeatsAeroClient([award]), state=state, notifier=notifier, heartbeat=heartbeat,
    )

    assert len(notifier.cash_sent) == 1
    fare_sent, verdict_sent, baseline_sent = notifier.cash_sent[0]
    assert fare_sent.price_usd == 4000.0
    assert verdict_sent.fire is True
    assert baseline_sent.ema_usd == 6000.0  # the PRE-drop baseline, not the just-updated one


def test_poller_does_not_fire_cash_drop_below_threshold():
    config = make_config()
    award = make_award()
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    run(
        config, cash_provider=FakeCashFareProvider([[make_fare(price_usd=6000.0)]]),
        seats_client=FakeSeatsAeroClient([award]), state=state, notifier=notifier, heartbeat=heartbeat,
    )
    _force_baseline_stale(state, "IAD", "FCO", "business", award.date)

    # $5,900 vs $6,000 baseline: ~1.7% / $100 -- nowhere near the 20%/$150 thresholds.
    run(
        config, cash_provider=FakeCashFareProvider([[make_fare(price_usd=5900.0)]]),
        seats_client=FakeSeatsAeroClient([award]), state=state, notifier=notifier, heartbeat=heartbeat,
    )

    assert notifier.cash_sent == []


def test_poller_dedups_repeat_cash_drop_alerts():
    """The same cash drop (same $50 price bucket) recurring on a later,
    still-stale-triggered run must not re-alert -- cash alerts go through
    the same dedup mechanism as award alerts."""
    config = make_config()
    award = make_award()
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    run(
        config, cash_provider=FakeCashFareProvider([[make_fare(price_usd=6000.0)]]),
        seats_client=FakeSeatsAeroClient([award]), state=state, notifier=notifier, heartbeat=heartbeat,
    )
    _force_baseline_stale(state, "IAD", "FCO", "business", award.date)
    run(
        config, cash_provider=FakeCashFareProvider([[make_fare(price_usd=4000.0)]]),
        seats_client=FakeSeatsAeroClient([award]), state=state, notifier=notifier, heartbeat=heartbeat,
    )
    assert len(notifier.cash_sent) == 1
    assert state.already_alerted(cash_key(make_fare(price_usd=4000.0))) is True

    # Same $4,000 price again, still forced stale so the poller re-checks --
    # must be suppressed as a duplicate, not re-sent.
    _force_baseline_stale(state, "IAD", "FCO", "business", award.date)
    run(
        config, cash_provider=FakeCashFareProvider([[make_fare(price_usd=4000.0)]]),
        seats_client=FakeSeatsAeroClient([award]), state=state, notifier=notifier, heartbeat=heartbeat,
    )

    assert len(notifier.cash_sent) == 1  # unchanged


# --- is_cash_below_mistake_fare_ceiling: third, independent cash trigger ---


def test_poller_fires_mistake_fare_ceiling_on_first_observation_no_baseline_needed():
    """Unlike is_cash_price_drop, the absolute ceiling trigger must fire even
    on a route's very FIRST observation -- no pre-existing baseline
    required, unlike every other cash trigger test in this file."""
    config = make_config()
    award = make_award()
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    run(
        config, cash_provider=FakeCashFareProvider([[make_fare(price_usd=150.0)]]),  # under the $200 default ceiling
        seats_client=FakeSeatsAeroClient([award]), state=state, notifier=notifier, heartbeat=heartbeat,
    )

    assert len(notifier.cash_sent) == 1
    fare_sent, verdict_sent, baseline_sent = notifier.cash_sent[0]
    assert fare_sent.price_usd == 150.0
    assert verdict_sent.reason == "possible mistake fare (absolute ceiling)"
    assert baseline_sent is None  # no baseline existed yet -- must not crash or fabricate one


def test_poller_mistake_fare_ceiling_dedups_like_cash_drop():
    config = make_config()
    award = make_award()
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    run(
        config, cash_provider=FakeCashFareProvider([[make_fare(price_usd=150.0)]]),
        seats_client=FakeSeatsAeroClient([award]), state=state, notifier=notifier, heartbeat=heartbeat,
    )
    assert len(notifier.cash_sent) == 1

    _force_baseline_stale(state, "IAD", "FCO", "business", award.date)
    run(
        config, cash_provider=FakeCashFareProvider([[make_fare(price_usd=150.0)]]),  # same $50-bucket price again
        seats_client=FakeSeatsAeroClient([award]), state=state, notifier=notifier, heartbeat=heartbeat,
    )

    assert len(notifier.cash_sent) == 1  # unchanged -- deduped, not re-sent


def test_poller_mistake_fare_ceiling_does_not_fire_above_ceiling_on_first_observation():
    config = make_config()
    award = make_award()
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    run(
        config, cash_provider=FakeCashFareProvider([[make_fare(price_usd=6000.0)]]),  # well above the ceiling
        seats_client=FakeSeatsAeroClient([award]), state=state, notifier=notifier, heartbeat=heartbeat,
    )

    assert notifier.cash_sent == []


def test_poller_mistake_fare_ceiling_takes_priority_over_relative_drop_no_double_alert():
    """If a price is BOTH under the absolute ceiling AND a big relative drop
    from baseline, only ONE cash alert must send for that fare observation
    -- not two -- since both triggers share the same dedup key."""
    config = make_config()
    award = make_award()
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    # Seed a baseline at $300 (above the $200 ceiling -- the seed itself
    # must not trigger the ceiling).
    run(
        config, cash_provider=FakeCashFareProvider([[make_fare(price_usd=300.0)]]),
        seats_client=FakeSeatsAeroClient([award]), state=state, notifier=notifier, heartbeat=heartbeat,
    )
    assert notifier.cash_sent == []
    _force_baseline_stale(state, "IAD", "FCO", "business", award.date)

    # $150 is BOTH a 50% drop from the $300 baseline (clears min_drop_pct)
    # AND under the $200 absolute ceiling -- must produce exactly one alert.
    run(
        config, cash_provider=FakeCashFareProvider([[make_fare(price_usd=150.0)]]),
        seats_client=FakeSeatsAeroClient([award]), state=state, notifier=notifier, heartbeat=heartbeat,
    )

    assert len(notifier.cash_sent) == 1
    _, verdict_sent, _ = notifier.cash_sent[0]
    assert verdict_sent.reason == "possible mistake fare (absolute ceiling)"  # ceiling took priority


# --- observability: log which programs actually appear in real Cached
# Search results, even when later rejected/deduped/capped ---


def test_poll_route_logs_programs_seen_in_cached_search_results(caplog):
    config = make_config()
    route = config.routes[0]
    awards = [make_award(program="aeroplan"), make_award(program="united", availability_id="united-iad-fco-2026-05-15", date=datetime.date(2026, 5, 15))]

    with caplog.at_level(logging.INFO, logger="poller"):
        from src.poller import poll_route

        poll_route(
            FakeSeatsAeroClient(awards), config.origins, route, config, InMemoryStateStore(), FakeNotifier(),
            cash_provider=FakeCashFareProvider(),
        )

    summary = next(r.getMessage() for r in caplog.records if "Cached Search hit" in r.getMessage())
    assert "aeroplan" in summary
    assert "united" in summary


def test_poll_route_logs_programs_seen_even_when_ineligible_and_rejected(caplog):
    """The observability log must show EVERY program seen, not just the ones
    that pass eligible_programs -- so an owner can see a program seats.aero
    tracks that isn't on the eligible list at all, not just measure hits
    among already-eligible ones."""
    config = dataclasses.replace(make_config(), eligible_programs=["united"])  # aeroplan excluded
    route = config.routes[0]
    award = make_award(program="aeroplan")

    with caplog.at_level(logging.INFO, logger="poller"):
        from src.poller import poll_route

        poll_route(
            FakeSeatsAeroClient([award]), config.origins, route, config, InMemoryStateStore(), FakeNotifier(),
            cash_provider=FakeCashFareProvider(),
        )

    summary = next(r.getMessage() for r in caplog.records if "Cached Search hit" in r.getMessage())
    assert "aeroplan" in summary  # logged even though it was then rejected by eligible_programs


class RaisingCashFareProvider:
    def search(self, *args, **kwargs):
        raise RuntimeError("SerpApi exploded")

    def close(self) -> None:
        pass


def test_poller_skips_award_when_cash_provider_raises(caplog):
    """Per flight-cash-price-monitor: 'a provider hiccup should log and
    skip, not crash the whole poll run' -- the run itself must not blow up.
    But a cash lookup failure must NOT degrade to v1.0's award-only
    fallback (comparable_cash_usd=None -> fire on cabin match alone)
    either: that fallback direction was retired as a real safety issue,
    since it meant a cash-provider outage's failure mode was MORE alerts,
    not fewer -- exactly backwards for a system whose top priority is
    avoiding alert fatigue. A cash-provider failure must now result in
    ZERO alerts (skip + a clear log line), not a fallback burst."""
    config = make_config()
    award = make_award()
    seats_client = FakeSeatsAeroClient([award])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    with caplog.at_level(logging.WARNING, logger="poller"):
        alerts_sent = run(
            config, cash_provider=RaisingCashFareProvider(), seats_client=seats_client, state=state,
            notifier=notifier, heartbeat=heartbeat,
        )

    assert alerts_sent == 0
    assert notifier.sent == []
    assert notifier.cash_sent == []
    assert seats_client.get_trips_calls == []  # never even reached Get Trips

    warning = next(r.getMessage() for r in caplog.records if "cash baseline lookup failed" in r.getMessage())
    assert "skipping rather than firing blind" in warning


def test_poller_skips_when_get_trips_shows_space_gone():
    config = make_config()
    award = make_award()
    seats_client = FakeSeatsAeroClient([award])
    seats_client.get_trips = lambda availability_id: None  # space vanished by the time we checked
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(config, cash_provider=FakeCashFareProvider(), seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat)

    assert alerts_sent == 0
    assert notifier.sent == []
    # not recorded as alerted, since nothing was actually sent
    assert state.already_alerted(award_key(award)) is False


def test_poller_does_not_heartbeat_on_auth_failure():
    from src.providers.seats_aero import SeatsAeroAuthError
    import pytest

    class FailingSeatsAeroClient(FakeSeatsAeroClient):
        def cached_search(self, *args, **kwargs):
            raise SeatsAeroAuthError("403")

    config = make_config()
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    with pytest.raises(SeatsAeroAuthError):
        run(config, cash_provider=FakeCashFareProvider(), seats_client=FailingSeatsAeroClient([]), state=state, notifier=notifier, heartbeat=heartbeat)

    assert heartbeat.emitted == 0


def test_poller_skips_alert_when_real_taxes_fail_recheck(monkeypatch):
    """Simulates the v1.1 scenario the post-Get-Trips recheck exists to
    guard against: an award clears the gate on the first pass using Cached
    Search's taxes (which can be stale -- here simulated as $0, e.g. not yet
    recrawled) yet should be rejected once Get Trips' fresher, more
    authoritative taxes are known and turn out higher. is_high_value is
    stubbed so this doesn't need a real cash provider to prove the *wiring*
    -- two calls happen, and the second one's verdict is what actually gates
    the send -- works correctly."""
    import src.poller as poller_module
    from src.valuation import Verdict

    def fake_is_high_value(award, config, wanted_cabins, comparable_cash_usd=None, taxes_usd=None):
        if taxes_usd and taxes_usd > 0:
            return Verdict(False, "real taxes push trip value below floor", "")
        return Verdict(True, "optimistic estimate", "estimate")

    monkeypatch.setattr(poller_module, "is_high_value", fake_is_high_value)

    config = make_config()
    # Cached Search's taxes_usd=0.0 here stands in for a stale/under-reported
    # figure; FakeSeatsAeroClient.get_trips always returns TotalTaxes=18000
    # (=$180) regardless, so the recheck's taxes_usd is > 0, tripping the
    # stub's rejection branch.
    award = make_award(taxes_usd=0.0)
    seats_client = FakeSeatsAeroClient([award])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(config, cash_provider=FakeCashFareProvider(), seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat)

    assert alerts_sent == 0
    assert notifier.sent == []
    assert seats_client.get_trips_calls == ["aeroplan-iad-fco-2026-05-14"]  # Get Trips was still called
    assert state.already_alerted(award_key(award)) is False  # never recorded, since nothing was sent


class _SpyNotifier:
    """Stand-in for DiscordNotifier/TelegramNotifier that records its own
    constructor args instead of touching the network -- used to verify
    _build_notifier() picks the right class/credentials from config.notifier."""

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.closed = False

    def send_award_alert(self, *args, **kwargs) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_run_defaults_to_discord_notifier_from_config(monkeypatch):
    """config.notifier defaults to 'discord' -- confirm run() actually picks
    DiscordNotifier (with the real webhook-url secret) when the caller
    doesn't inject a notifier, rather than always reaching for Telegram."""
    import src.poller as poller_module

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/fake-token")
    spies = []
    monkeypatch.setattr(
        poller_module,
        "DiscordNotifier",
        lambda webhook_url: spies.append(_SpyNotifier(webhook_url=webhook_url)) or spies[-1],
    )

    config = make_config()
    assert config.notifier == "discord"

    run(config, cash_provider=FakeCashFareProvider(), seats_client=FakeSeatsAeroClient([]), state=InMemoryStateStore(), heartbeat=FakeHeartbeat())

    assert len(spies) == 1
    assert spies[0].init_kwargs == {"webhook_url": "https://discord.com/api/webhooks/fake/fake-token"}
    assert spies[0].closed is True  # run() owns and closes notifiers it builds itself


def test_run_selects_telegram_notifier_when_configured(monkeypatch):
    """Swapping notifier: telegram in watchlist.yaml must route to
    TelegramNotifier without any code change -- the whole point of the
    config knob."""
    import src.poller as poller_module

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-chat-id")
    spies = []
    monkeypatch.setattr(
        poller_module,
        "TelegramNotifier",
        lambda bot_token, chat_id: spies.append(_SpyNotifier(bot_token=bot_token, chat_id=chat_id)) or spies[-1],
    )

    config = dataclasses.replace(make_config(), notifier="telegram")

    run(config, cash_provider=FakeCashFareProvider(), seats_client=FakeSeatsAeroClient([]), state=InMemoryStateStore(), heartbeat=FakeHeartbeat())

    assert len(spies) == 1
    assert spies[0].init_kwargs == {"bot_token": "fake-token", "chat_id": "fake-chat-id"}


def test_run_rejects_unknown_notifier_name():
    import pytest

    config = dataclasses.replace(make_config(), notifier="carrier-pigeon")

    with pytest.raises(ValueError, match="carrier-pigeon"):
        run(config, cash_provider=FakeCashFareProvider(), seats_client=FakeSeatsAeroClient([]), state=InMemoryStateStore(), heartbeat=FakeHeartbeat())


class _SpyState:
    """Stand-in for DynamoStateStore that records its own constructor args
    instead of touching real DynamoDB -- used to verify run() sources table
    names from the environment, not a hardcoded default."""

    def __init__(self, alerts_table, baselines_table):
        self.alerts_table = alerts_table
        self.baselines_table = baselines_table

    def already_alerted(self, key: str) -> bool:
        return False

    def record_alert(self, key: str, ttl_seconds: int) -> None:
        pass

    def get_baseline(self, route_key: str):
        return None

    def update_baseline(self, route_key: str, price: float) -> None:
        pass


def test_run_builds_dynamo_state_store_from_env_vars_not_hardcoded_names(monkeypatch):
    """Regression: run() used to hardcode DynamoStateStore(alerts_table=
    "flight-deal-alerts", baselines_table="flight-deal-baselines") -- stale
    names from before the project was renamed to flight-tracker-app. In
    production this 403'd (AccessDeniedException on dynamodb:GetItem)
    instead of 404ing, because IAM (infra/iam.tf) only grants access to the
    real Terraform-created tables (infra/dynamodb.tf), and nothing had ever
    granted access to a table nobody asked for. infra/lambda.tf already sets
    ALERTS_TABLE_NAME/BASELINES_TABLE_NAME from the real
    aws_dynamodb_table.*.name references -- run() must actually read them."""
    import src.poller as poller_module

    monkeypatch.setenv("ALERTS_TABLE_NAME", "flight-tracker-app-alerts")
    monkeypatch.setenv("BASELINES_TABLE_NAME", "flight-tracker-app-baselines")

    spies = []
    monkeypatch.setattr(
        poller_module,
        "DynamoStateStore",
        lambda alerts_table, baselines_table: spies.append(
            _SpyState(alerts_table=alerts_table, baselines_table=baselines_table)
        )
        or spies[-1],
    )

    config = make_config()
    run(config, cash_provider=FakeCashFareProvider(), seats_client=FakeSeatsAeroClient([]), notifier=FakeNotifier(), heartbeat=FakeHeartbeat())

    assert len(spies) == 1
    assert spies[0].alerts_table == "flight-tracker-app-alerts"
    assert spies[0].baselines_table == "flight-tracker-app-baselines"


def test_run_raises_clear_error_when_table_name_env_vars_missing(monkeypatch):
    """No silent fallback to a guessed/default table name -- missing env vars
    must fail loud and name exactly what's missing, matching src/secrets.py's
    convention for required config."""
    import pytest

    monkeypatch.delenv("ALERTS_TABLE_NAME", raising=False)
    monkeypatch.delenv("BASELINES_TABLE_NAME", raising=False)

    config = make_config()
    with pytest.raises(RuntimeError, match="ALERTS_TABLE_NAME"):
        run(config, cash_provider=FakeCashFareProvider(), seats_client=FakeSeatsAeroClient([]), notifier=FakeNotifier(), heartbeat=FakeHeartbeat())


# --- eligible_programs prefilter, per-route origins override, no-cash-data skip ---


def test_poller_skips_ineligible_program_before_any_provider_call():
    """eligible_programs must reject a candidate BEFORE a cash lookup or Get
    Trips call is ever spent on it -- assert call counts, not just the
    outcome, since a version that filtered too late (e.g. after the cash
    lookup) would still produce alerts_sent == 0 but waste real provider
    calls in production."""
    config = dataclasses.replace(make_config(), eligible_programs=["united", "delta"])
    award = make_award(program="aeroplan")  # not in eligible_programs
    seats_client = FakeSeatsAeroClient([award])
    cash_provider = FakeCashFareProvider([[make_fare(price_usd=5900.0)]])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(
        config, cash_provider=cash_provider, seats_client=seats_client, state=state, notifier=notifier,
        heartbeat=heartbeat,
    )

    assert alerts_sent == 0
    assert notifier.sent == []
    assert cash_provider.calls == []  # no cash lookup was ever attempted
    assert seats_client.get_trips_calls == []  # no Get Trips call was ever attempted


def test_poller_fires_eligible_program_when_eligible_programs_set():
    config = dataclasses.replace(make_config(), eligible_programs=["united", "aeroplan"])
    award = make_award(program="aeroplan")  # in eligible_programs
    seats_client = FakeSeatsAeroClient([award])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(
        config, cash_provider=FakeCashFareProvider(default_fare=make_fare(price_usd=5900.0)),
        seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat,
    )

    assert alerts_sent == 1


# --- premium_cabin_max_multiplier: free sanity prefilter on business/first ---


def test_poller_skips_premium_cabin_candidate_exceeding_multiplier_before_any_provider_call():
    """premium_cabin_max_multiplier must reject a business/first candidate
    BEFORE a cash lookup or Get Trips call is ever spent on it -- assert call
    counts, not just the outcome, matching
    test_poller_skips_ineligible_program_before_any_provider_call's pattern:
    a version that filtered too late (e.g. after the cash lookup) would still
    produce alerts_sent == 0 but waste a real provider call in production."""
    config = make_config()  # premium_cabin_max_multiplier defaults to 2.0
    # economy_miles=30000, business miles=88000 -> 2.93x, over the 2.0x default.
    award = make_award(cabin="business", miles=88000, economy_miles=30000)
    seats_client = FakeSeatsAeroClient([award])
    cash_provider = FakeCashFareProvider([[make_fare(price_usd=5900.0)]])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(
        config, cash_provider=cash_provider, seats_client=seats_client, state=state, notifier=notifier,
        heartbeat=heartbeat,
    )

    assert alerts_sent == 0
    assert notifier.sent == []
    assert cash_provider.calls == []  # no cash lookup was ever attempted
    assert seats_client.get_trips_calls == []  # no Get Trips call was ever attempted


def test_poller_fires_premium_cabin_candidate_within_multiplier():
    config = make_config()
    # economy_miles=60000, business miles=88000 -> 1.47x, within the 2.0x default.
    award = make_award(cabin="business", miles=88000, economy_miles=60000)
    seats_client = FakeSeatsAeroClient([award])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(
        config, cash_provider=FakeCashFareProvider(default_fare=make_fare(price_usd=5900.0)),
        seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat,
    )

    assert alerts_sent == 1


def test_poller_does_not_apply_premium_cabin_multiplier_to_economy():
    # An economy candidate must never be rejected by the ratio check, even
    # with no economy_miles data of its own (it IS the economy record).
    # make_config()'s route only tracks business/first by default -- widen it
    # to include economy so this test isn't rejected by the CABIN check
    # instead of proving anything about the ratio check.
    base_config = make_config()
    economy_route = dataclasses.replace(base_config.routes[0], cabins=["economy", "business", "first"])
    config = dataclasses.replace(base_config, routes=[economy_route])
    award = make_award(cabin="economy", miles=20000, economy_miles=None, taxes_usd=75.0)
    seats_client = FakeSeatsAeroClient([award])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(
        config, cash_provider=FakeCashFareProvider(default_fare=make_fare(price_usd=5900.0)),
        seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat,
    )

    assert alerts_sent == 1


# --- transfer_bonus_pct: informational only, surfaced to the notifier ---


def test_poller_passes_transfer_bonus_pct_to_notifier_when_configured():
    base_config = make_config()
    config = dataclasses.replace(
        base_config, awards=dataclasses.replace(base_config.awards, transfer_bonus_pct={"aeroplan": 0.25}),
    )
    award = make_award()  # program="aeroplan"
    seats_client = FakeSeatsAeroClient([award])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    run(
        config, cash_provider=FakeCashFareProvider(default_fare=make_fare(price_usd=5900.0)),
        seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat,
    )

    assert len(notifier.sent) == 1
    transfer_bonus_pct = notifier.sent[0][4]
    assert transfer_bonus_pct == 0.25


def test_poller_passes_zero_transfer_bonus_pct_when_program_not_listed():
    # aeroplan has no entry in transfer_bonus_pct -- bonus_pct() must default
    # to 0.0, not raise or silently pick some other program's value.
    config = make_config()  # transfer_bonus_pct defaults to {} -> bonus_pct() always 0.0
    award = make_award()  # program="aeroplan"
    seats_client = FakeSeatsAeroClient([award])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    run(
        config, cash_provider=FakeCashFareProvider(default_fare=make_fare(price_usd=5900.0)),
        seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat,
    )

    assert len(notifier.sent) == 1
    assert notifier.sent[0][4] == 0.0


# --- group-winner selection: prevents near-duplicate dates of one deal
# from crowding the per-run alert cap -- see .claude/skills/deal-valuation's
# winner-selection spec and src/valuation.py's select_group_winners ---


def test_poll_route_group_winner_selection_only_confirms_highest_cpp_of_four_same_month_candidates():
    """4 same route/cabin/program candidates, different dates within the
    SAME calendar month -- only the single highest-cpp one (d2, cheapest
    miles here) should reach Get Trips/exact-confirm/notify. The other 3
    must be accounted for as grouped_out -- NOT sent, NOT capped, NOT
    confirmed (no Get Trips call spent on any of them)."""
    from src.poller import poll_route

    config = make_config()
    route = config.routes[0]
    awards = [
        make_award(availability_id="d1", date=datetime.date(2026, 8, 5), miles=90000, economy_miles=60000),
        make_award(availability_id="d2", date=datetime.date(2026, 8, 12), miles=60000, economy_miles=60000),
        make_award(availability_id="d3", date=datetime.date(2026, 8, 20), miles=95000, economy_miles=60000),
        make_award(availability_id="d4", date=datetime.date(2026, 8, 27), miles=110000, economy_miles=60000),
    ]
    seats_client = FakeSeatsAeroClient(awards)
    cash_provider = FakeCashFareProvider(default_fare=make_fare(price_usd=5900.0))
    state = InMemoryStateStore()
    notifier = FakeNotifier()

    stats = poll_route(seats_client, config.origins, route, config, state, notifier, cash_provider=cash_provider)

    assert stats.skipped_grouped_out == 3
    assert stats.skipped_capped == 0
    assert stats.skipped_duplicate == 0
    assert stats.alerts_sent == 1
    assert len(notifier.sent) == 1
    assert notifier.sent[0][0].availability_id == "d2"  # lowest miles -> highest cpp, at fixed cash price
    assert seats_client.get_trips_calls == ["d2"]  # Get Trips spent ONLY on the winner


def test_poll_route_group_winner_selection_is_independent_per_program():
    """Mixed case: an Aeroplan IAD-FCO candidate and a Virgin Atlantic
    IAD-FCO candidate, same month -- both must fire independently, proving
    grouping is scoped per PROGRAM, not just per route/cabin/date."""
    from src.poller import poll_route

    base_config = make_config()
    config = dataclasses.replace(
        base_config,
        awards=dataclasses.replace(
            base_config.awards, cpp_floors={"default": 1.4, "aeroplan": 1.5, "virginatlantic": 1.5},
        ),
    )
    route = config.routes[0]

    aeroplan_award = make_award(availability_id="aeroplan-award", program="aeroplan", date=datetime.date(2026, 8, 10))
    virgin_award = make_award(
        availability_id="virgin-award", program="virginatlantic", date=datetime.date(2026, 8, 15),
    )
    seats_client = FakeSeatsAeroClient([aeroplan_award, virgin_award])
    cash_provider = FakeCashFareProvider(default_fare=make_fare(price_usd=5900.0))
    state = InMemoryStateStore()
    notifier = FakeNotifier()

    stats = poll_route(seats_client, config.origins, route, config, state, notifier, cash_provider=cash_provider)

    assert stats.skipped_grouped_out == 0
    assert stats.alerts_sent == 2
    sent_ids = {sent[0].availability_id for sent in notifier.sent}
    assert sent_ids == {"aeroplan-award", "virgin-award"}


def test_poll_route_group_winner_selection_produces_two_winners_across_different_months():
    """Regression: collapsing an ENTIRE ~150-day window down to a single
    winner per route would be too aggressive -- dates a month or more apart
    are genuinely different trip options. Same route/cabin/program, one
    qualifying date in August and one in October, must produce TWO winners
    (two real alerts), not one."""
    from src.poller import poll_route

    config = make_config()
    route = config.routes[0]
    august_award = make_award(availability_id="august-award", date=datetime.date(2026, 8, 10))
    october_award = make_award(availability_id="october-award", date=datetime.date(2026, 10, 15))
    seats_client = FakeSeatsAeroClient([august_award, october_award])
    cash_provider = FakeCashFareProvider(default_fare=make_fare(price_usd=5900.0))
    state = InMemoryStateStore()
    notifier = FakeNotifier()

    stats = poll_route(seats_client, config.origins, route, config, state, notifier, cash_provider=cash_provider)

    assert stats.skipped_grouped_out == 0
    assert stats.alerts_sent == 2
    sent_ids = {sent[0].availability_id for sent in notifier.sent}
    assert sent_ids == {"august-award", "october-award"}


def test_poll_route_sends_other_dates_annotation_when_group_has_losers():
    """The winning alert must name the other qualifying dates in its own
    (origin, destination, cabin, program, month) group that it beat --
    Notifier.send_award_alert's group_other_dates kwarg, sorted ascending,
    excluding the winner's own date."""
    from src.poller import poll_route

    config = make_config()
    route = config.routes[0]
    awards = [
        make_award(availability_id="d1", date=datetime.date(2026, 8, 5), miles=90000, economy_miles=60000),
        make_award(availability_id="d2", date=datetime.date(2026, 8, 12), miles=60000, economy_miles=60000),  # winner
        make_award(availability_id="d3", date=datetime.date(2026, 8, 20), miles=95000, economy_miles=60000),
    ]
    seats_client = FakeSeatsAeroClient(awards)
    cash_provider = FakeCashFareProvider(default_fare=make_fare(price_usd=5900.0))
    state = InMemoryStateStore()
    notifier = FakeNotifier()

    poll_route(seats_client, config.origins, route, config, state, notifier, cash_provider=cash_provider)

    assert len(notifier.sent) == 1
    group_other_dates = notifier.sent[0][5]
    assert group_other_dates == [datetime.date(2026, 8, 5), datetime.date(2026, 8, 20)]


def test_poll_route_sends_no_other_dates_annotation_when_group_has_no_losers():
    """A group of exactly one candidate must NOT show the annotation at
    all -- empty/None, never a "+0 other dates" line."""
    from src.poller import poll_route

    config = make_config()
    route = config.routes[0]
    award = make_award()
    seats_client = FakeSeatsAeroClient([award])
    cash_provider = FakeCashFareProvider(default_fare=make_fare(price_usd=5900.0))
    state = InMemoryStateStore()
    notifier = FakeNotifier()

    poll_route(seats_client, config.origins, route, config, state, notifier, cash_provider=cash_provider)

    assert len(notifier.sent) == 1
    assert notifier.sent[0][5] == []


def test_poller_uses_route_origins_override_not_top_level():
    """A route with its own `origins` override must be queried using THAT
    origin list, not the top-level config.origins -- see RouteConfig.origins
    and run()'s per-route resolution."""
    config = make_config()
    overridden_route = dataclasses.replace(config.routes[0], origins=["BWI"])
    config = dataclasses.replace(config, routes=[overridden_route])
    award = make_award(origin="BWI")
    seats_client = FakeSeatsAeroClient([award])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(
        config, cash_provider=FakeCashFareProvider(default_fare=make_fare(price_usd=5900.0)),
        seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat,
    )

    assert seats_client.cached_search_origins == ["BWI"]  # route override used, not config.origins ("IAD")
    assert alerts_sent == 1


def test_poller_route_without_origins_override_falls_back_to_top_level():
    config = make_config()  # config.routes[0].origins is None -> falls back to config.origins == ["IAD"]
    award = make_award()  # origin="IAD"
    seats_client = FakeSeatsAeroClient([award])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    run(config, cash_provider=FakeCashFareProvider(), seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat)

    assert seats_client.cached_search_origins == ["IAD"]


def test_poller_no_cash_data_skip_applies_unconditionally_to_every_route():
    """There is no route-level opt-out of the no-cash-data skip anymore --
    it used to be conditional on a per-route require_cash_comparison flag
    (removed; see src/valuation.py's is_high_value module docstring). This
    uses the default route config, with no special flag of any kind, and
    confirms the skip still applies -- the SAME assertion as
    test_poller_skips_when_no_cash_provider_data_at_all above, restated
    here specifically to prove universality, not conditionality."""
    config = make_config()
    award = make_award()
    seats_client = FakeSeatsAeroClient([award])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(
        config, cash_provider=FakeCashFareProvider(), seats_client=seats_client, state=state, notifier=notifier,
        heartbeat=heartbeat,
    )

    assert alerts_sent == 0
    assert notifier.sent == []


def test_run_does_not_close_an_injected_notifier():
    """An explicitly-passed notifier (as every other test in this file does)
    is caller-owned -- run() must not close it out from under the caller."""
    config = make_config()
    notifier = FakeNotifier()

    run(config, cash_provider=FakeCashFareProvider(), seats_client=FakeSeatsAeroClient([]), state=InMemoryStateStore(), notifier=notifier, heartbeat=FakeHeartbeat())

    assert notifier.closed is False


# --- structural drift-proofing: poll_route() and scripts/dry_run.py must
# call the SAME shared classify_candidate()/finish_award_candidate() pair,
# never a second copy of either ---


# --- lambda_handler mode dispatch: {"mode": "digest"} routes to run_digest(),
# every other event preserves the pre-digest real-time path exactly ---


def test_lambda_handler_dispatches_to_run_digest_on_digest_mode(monkeypatch):
    import src.poller as poller_module
    from src.digest import DigestResult

    calls = {"run": 0, "run_digest": 0}

    def fake_run_digest(*args, **kwargs):
        calls["run_digest"] += 1
        return DigestResult(cash_rank=[], cpp_rank=[], candidates_evaluated=3, candidates_ranked=1)

    def fake_run(*args, **kwargs):
        calls["run"] += 1
        return 0

    monkeypatch.setattr(poller_module, "run_digest", fake_run_digest)
    monkeypatch.setattr(poller_module, "run", fake_run)

    response = poller_module.lambda_handler({"mode": "digest"}, None)

    assert calls == {"run": 0, "run_digest": 1}  # the real-time path was never touched
    assert response == {"statusCode": 200, "mode": "digest", "candidatesEvaluated": 3, "candidatesRanked": 1}


def test_lambda_handler_default_path_is_byte_for_byte_unchanged(monkeypatch):
    """Regression guard: adding the digest dispatch branch must not alter
    lambda_handler's existing return shape or behavior for every event that
    ISN'T {"mode": "digest"} -- a missing event, an empty dict, and the
    real-time schedule's actual (non-digest) payload all must produce the
    exact same {"statusCode": 200, "alertsSent": N} response run() always
    produced, with run_digest() never even constructed."""
    import src.poller as poller_module
    from src.digest import DigestResult

    calls = {"run": 0, "run_digest": 0}

    def fake_run_digest(*args, **kwargs):
        calls["run_digest"] += 1
        return DigestResult(cash_rank=[], cpp_rank=[], candidates_evaluated=0, candidates_ranked=0)

    def fake_run(*args, **kwargs):
        calls["run"] += 1
        return 5

    monkeypatch.setattr(poller_module, "run_digest", fake_run_digest)
    monkeypatch.setattr(poller_module, "run", fake_run)

    for event in (None, {}, {"mode": "realtime"}, {"some": "other-payload"}):
        calls["run"] = calls["run_digest"] = 0
        response = poller_module.lambda_handler(event, None)
        assert response == {"statusCode": 200, "alertsSent": 5}
        assert calls == {"run": 1, "run_digest": 0}


def test_dry_run_script_calls_the_same_classify_and_finish_functions_as_poll_route():
    """This is an object-identity check, not a behavioral one on purpose.
    A future edit could reintroduce a second, textually-similar copy of
    classify_candidate()/finish_award_candidate() inside scripts/dry_run.py
    (e.g. someone "just inlines a tweak" for a one-off test) that still
    passes every behavioral test in this file, since it would still produce
    plausible-looking output -- exactly the failure mode that let this
    script drift out of sync with production twice before (a missed
    trips[0] regression, and later missing the entire v1.1 cash wiring).
    Asserting object identity (`is`, not `==`) fails loudly the moment
    scripts/dry_run.py stops calling src.poller's shared functions
    themselves, regardless of how similar a replacement looks."""
    import importlib.util
    from pathlib import Path

    import src.poller as poller_module

    dry_run_path = Path(__file__).resolve().parent.parent / "scripts" / "dry_run.py"
    spec = importlib.util.spec_from_file_location("dry_run_under_test", dry_run_path)
    dry_run_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dry_run_module)  # runs module-level code only -- main() is __name__-guarded, never called

    assert dry_run_module.classify_candidate is poller_module.classify_candidate
    assert dry_run_module.finish_award_candidate is poller_module.finish_award_candidate
    # Same check for the group-winner-selection function both this script
    # and poll_route() call on the shared function's output, plus the other
    # pieces of the shared contract dry_run.py depends on -- PollStats (the
    # mutable aggregation object), TripFetchResult (the fetch_trip
    # callback's return type), and ClassifyResult (classify_candidate's
    # return type) -- a redefinition of any of these, even an identical-
    # looking one, would silently break the shared function's actual
    # contract.
    assert dry_run_module.select_group_winners is poller_module.select_group_winners
    assert dry_run_module.PollStats is poller_module.PollStats
    assert dry_run_module.TripFetchResult is poller_module.TripFetchResult
    assert dry_run_module.ClassifyResult is poller_module.ClassifyResult
