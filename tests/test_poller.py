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

    def cached_search(self, origin, destinations, start, end, cabins) -> list[AwardAvailability]:
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
    """Defaults to finding nothing (empty search results) every call, so
    existing award-only tests get v1.0-equivalent behavior (no cash data,
    comparable_cash_usd stays None) without each one having to care about
    cash. Tests that DO care about cash pass fares_by_call explicitly."""

    def __init__(self, fares_by_call: list[list[CashFare]] | None = None):
        self.calls: list[tuple] = []
        self._fares_by_call = fares_by_call or []

    def search(self, origin, destinations, start, end, cabin) -> list[CashFare]:
        self.calls.append((origin, destinations, start, end, cabin))
        idx = len(self.calls) - 1
        return self._fares_by_call[idx] if idx < len(self._fares_by_call) else []

    def close(self) -> None:
        pass


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple] = []  # (award, verdict, trip, deep_link)
        self.cash_sent: list[tuple] = []  # (fare, verdict, baseline)
        self.closed = False

    def send_award_alert(self, award, verdict, trip, *, deep_link=None) -> None:
        self.sent.append((award, verdict, trip, deep_link))

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

    alerts_sent = run(config, cash_provider=FakeCashFareProvider(), seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat)

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

    run(config, cash_provider=FakeCashFareProvider(), seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat)

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

    first_run = run(config, cash_provider=FakeCashFareProvider(), seats_client=FakeSeatsAeroClient([award]), state=state, notifier=notifier, heartbeat=heartbeat)
    second_run = run(config, cash_provider=FakeCashFareProvider(), seats_client=FakeSeatsAeroClient([award]), state=state, notifier=notifier, heartbeat=heartbeat)

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
    run(config, cash_provider=FakeCashFareProvider(), seats_client=FakeSeatsAeroClient([first]), state=state, notifier=notifier, heartbeat=heartbeat)

    better = make_award(miles=60000, availability_id="aeroplan-iad-fco-2026-05-14-v2")
    second_alerts = run(config, cash_provider=FakeCashFareProvider(), seats_client=FakeSeatsAeroClient([better]), state=state, notifier=notifier, heartbeat=heartbeat)

    assert second_alerts == 1
    assert len(notifier.sent) == 2


def make_distinct_awards(n: int) -> list[AwardAvailability]:
    """n distinct qualifying awards -- different dates/availability_ids AND
    different miles buckets, so each gets its own dedup key (award_key
    buckets by miles // 5000 * 5000) and none collide with each other."""
    return [
        make_award(
            availability_id=f"aeroplan-iad-fco-2026-05-{14 + i}",
            date=datetime.date(2026, 5, 14 + i),
            miles=88000 + i * 10000,
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

    alerts_sent = run(config, cash_provider=FakeCashFareProvider(), seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat)

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

    stats = poll_route(
        FakeSeatsAeroClient(awards), config.origins, route, config, state, notifier, max_alerts_per_run=3
    )
    assert stats.alerts_sent == 3
    assert stats.skipped_capped == 2
    assert stats.skipped_duplicate == 0

    stats2 = poll_route(
        FakeSeatsAeroClient(awards), config.origins, route, config, state, notifier, max_alerts_per_run=3
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
        run(config, cash_provider=FakeCashFareProvider(), seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat)

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


def test_poller_skips_confirm_and_uses_none_when_no_cash_provider_data_at_all():
    """When there's no real cash data to begin with (e.g. an empty
    provider, matching v1.0's no-cash-provider behavior), the exact-date
    confirm step must not run at all -- there's nothing to confirm, and
    running it anyway would spend a wasted call and could wrongly reject an
    award that's only ever meant to fire on cabin-match-alone semantics."""
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

    assert alerts_sent == 1  # fires on cabin-match-alone, same as v1.0
    # Only the weekly-bucket lookup call happens -- no second (confirm) call.
    assert len(cash_provider.calls) == 1


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


class RaisingCashFareProvider:
    def search(self, *args, **kwargs):
        raise RuntimeError("SerpApi exploded")

    def close(self) -> None:
        pass


def test_poller_continues_award_only_when_cash_provider_raises():
    """Per flight-cash-price-monitor: 'a provider hiccup should log and
    skip, not crash the whole poll run.' A cash lookup failure must degrade
    to v1.0's award-only behavior (comparable_cash_usd=None), not blow up
    the run or block the award from firing on cabin match alone."""
    config = make_config()
    award = make_award()
    seats_client = FakeSeatsAeroClient([award])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(
        config, cash_provider=RaisingCashFareProvider(), seats_client=seats_client, state=state,
        notifier=notifier, heartbeat=heartbeat,
    )

    assert alerts_sent == 1
    assert notifier.sent[0][0].origin == "IAD"
    assert notifier.cash_sent == []


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


def test_run_does_not_close_an_injected_notifier():
    """An explicitly-passed notifier (as every other test in this file does)
    is caller-owned -- run() must not close it out from under the caller."""
    config = make_config()
    notifier = FakeNotifier()

    run(config, cash_provider=FakeCashFareProvider(), seats_client=FakeSeatsAeroClient([]), state=InMemoryStateStore(), notifier=notifier, heartbeat=FakeHeartbeat())

    assert notifier.closed is False
