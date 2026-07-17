from __future__ import annotations

import dataclasses
import datetime

from src.providers.cash.base import CashFare
from src.state import EMA_ALPHA, InMemoryStateStore, award_key, baseline_key, cash_key


def test_award_key_buckets_similar_miles_together(saver_business_award):
    a = dataclasses.replace(saver_business_award, miles=88_000)
    b = dataclasses.replace(saver_business_award, miles=89_500)
    assert award_key(a) == award_key(b)


def test_award_key_differs_across_bucket_boundary(saver_business_award):
    a = dataclasses.replace(saver_business_award, miles=88_000)
    b = dataclasses.replace(saver_business_award, miles=92_000)
    assert award_key(a) != award_key(b)


def test_award_key_includes_route_cabin_program_date(saver_business_award):
    key = award_key(saver_business_award)
    assert key == "award:IAD-FCO:2026-05-14:business:aeroplan:85000"


def test_dedup_prevents_repeat_alert_within_ttl(saver_business_award):
    store = InMemoryStateStore()
    key = award_key(saver_business_award)
    assert store.already_alerted(key) is False
    store.record_alert(key, ttl_seconds=3600)
    assert store.already_alerted(key) is True


def test_dedup_expires_after_ttl(saver_business_award, monkeypatch):
    import time

    store = InMemoryStateStore()
    key = award_key(saver_business_award)
    fake_now = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: fake_now[0])

    store.record_alert(key, ttl_seconds=10)
    assert store.already_alerted(key) is True

    fake_now[0] += 20
    assert store.already_alerted(key) is False


def test_baseline_round_trip():
    store = InMemoryStateStore()
    assert store.get_baseline("IAD-FCO:economy") is None
    store.update_baseline("IAD-FCO:economy", 650.0)
    baseline = store.get_baseline("IAD-FCO:economy")
    assert baseline is not None
    assert baseline.trailing_min_usd == 650.0
    assert baseline.ema_usd == 650.0  # first observation -- both fields seed to the same price


def test_baseline_first_update_seeds_trailing_min_and_ema_equally():
    store = InMemoryStateStore()
    store.update_baseline("IAD-FCO:business", 6000.0)
    baseline = store.get_baseline("IAD-FCO:business")
    assert baseline.trailing_min_usd == baseline.ema_usd == 6000.0


def test_baseline_trailing_min_only_decreases():
    store = InMemoryStateStore()
    store.update_baseline("IAD-FCO:business", 6000.0)
    store.update_baseline("IAD-FCO:business", 6500.0)  # a higher observation
    baseline = store.get_baseline("IAD-FCO:business")
    assert baseline.trailing_min_usd == 6000.0  # unchanged -- min() never goes up


def test_baseline_trailing_min_updates_on_a_new_low():
    store = InMemoryStateStore()
    store.update_baseline("IAD-FCO:business", 6000.0)
    store.update_baseline("IAD-FCO:business", 5000.0)
    baseline = store.get_baseline("IAD-FCO:business")
    assert baseline.trailing_min_usd == 5000.0


def test_baseline_ema_blends_new_price_at_configured_alpha():
    store = InMemoryStateStore()
    store.update_baseline("IAD-FCO:business", 6000.0)
    store.update_baseline("IAD-FCO:business", 5000.0)
    baseline = store.get_baseline("IAD-FCO:business")
    expected_ema = EMA_ALPHA * 5000.0 + (1 - EMA_ALPHA) * 6000.0
    assert baseline.ema_usd == expected_ema


def test_cash_key_buckets_price_in_50_dollar_increments():
    a = CashFare(
        origin="IAD", destination="FCO", date=datetime.date(2026, 9, 14), return_date=None,
        cabin="business", price_usd=5980.0, airline="United", stops=0, deep_link=None,
    )
    b = dataclasses.replace(a, price_usd=5960.0)  # same $50 bucket
    c = dataclasses.replace(a, price_usd=5920.0)  # crosses into the bucket below
    assert cash_key(a) == cash_key(b)
    assert cash_key(a) != cash_key(c)


def test_cash_key_includes_route_cabin_date():
    fare = CashFare(
        origin="IAD", destination="FCO", date=datetime.date(2026, 9, 14), return_date=None,
        cabin="business", price_usd=5980.0, airline="United", stops=0, deep_link=None,
    )
    assert cash_key(fare) == "cash:IAD-FCO:2026-09-14:business:5950"


def test_baseline_key_buckets_by_iso_week_not_exact_date():
    # 2026-09-14 (Mon) and 2026-09-16 (Wed) fall in the same ISO week.
    monday = baseline_key("IAD", "FCO", "business", datetime.date(2026, 9, 14))
    wednesday = baseline_key("IAD", "FCO", "business", datetime.date(2026, 9, 16))
    next_monday = baseline_key("IAD", "FCO", "business", datetime.date(2026, 9, 21))
    assert monday == wednesday
    assert monday != next_monday
