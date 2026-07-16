"""End-to-end poller test with fake providers -- no real HTTP, no real creds."""

from __future__ import annotations

import datetime

from src.config import AwardConfig, CashConfig, AlertConfig, DateWindow, RouteConfig, ScheduleConfig, WatchlistConfig
from src.notify.base import Button
from src.poller import run
from src.providers.seats_aero import AwardAvailability
from src.state import InMemoryStateStore, award_key


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
                return [
                    {
                        "ID": f"{availability_id}-trip1",
                        "AvailabilityID": availability_id,
                        "MileageCost": hit.miles,
                        "TotalTaxes": 18000,
                        "Cabin": hit.cabin,
                        "RemainingSeats": hit.seats,
                        "FlightNumbers": "AC942",
                    }
                ]
        return None

    def close(self) -> None:
        pass


class FakeNotifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message: str, buttons: list[Button] | None = None) -> None:
        self.sent.append(message)


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

    run(config, seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat)

    assert calls[0] == 222.5  # first call used the award's real taxes, not a placeholder


def test_poller_sends_alert_for_business_award():
    config = make_config()
    award = make_award()
    seats_client = FakeSeatsAeroClient([award])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(config, seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat)

    assert alerts_sent == 1
    assert len(notifier.sent) == 1
    assert "IAD" in notifier.sent[0]
    assert seats_client.get_trips_calls == ["aeroplan-iad-fco-2026-05-14"]
    assert heartbeat.emitted == 1


def test_poller_skips_untracked_cabin():
    config = make_config()
    award = make_award(cabin="economy")
    seats_client = FakeSeatsAeroClient([award])
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(config, seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat)

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

    alerts_sent = run(config, seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat)

    assert alerts_sent == 0


def test_poller_dedups_on_second_run_same_deal():
    config = make_config()
    award = make_award()
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    first_run = run(config, seats_client=FakeSeatsAeroClient([award]), state=state, notifier=notifier, heartbeat=heartbeat)
    second_run = run(config, seats_client=FakeSeatsAeroClient([award]), state=state, notifier=notifier, heartbeat=heartbeat)

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
    run(config, seats_client=FakeSeatsAeroClient([first]), state=state, notifier=notifier, heartbeat=heartbeat)

    better = make_award(miles=60000, availability_id="aeroplan-iad-fco-2026-05-14-v2")
    second_alerts = run(config, seats_client=FakeSeatsAeroClient([better]), state=state, notifier=notifier, heartbeat=heartbeat)

    assert second_alerts == 1
    assert len(notifier.sent) == 2


def test_poller_skips_when_get_trips_shows_space_gone():
    config = make_config()
    award = make_award()
    seats_client = FakeSeatsAeroClient([award])
    seats_client.get_trips = lambda availability_id: None  # space vanished by the time we checked
    state = InMemoryStateStore()
    notifier = FakeNotifier()
    heartbeat = FakeHeartbeat()

    alerts_sent = run(config, seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat)

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
        run(config, seats_client=FailingSeatsAeroClient([]), state=state, notifier=notifier, heartbeat=heartbeat)

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

    alerts_sent = run(config, seats_client=seats_client, state=state, notifier=notifier, heartbeat=heartbeat)

    assert alerts_sent == 0
    assert notifier.sent == []
    assert seats_client.get_trips_calls == ["aeroplan-iad-fco-2026-05-14"]  # Get Trips was still called
    assert state.already_alerted(award_key(award)) is False  # never recorded, since nothing was sent
