from __future__ import annotations

import dataclasses

from src.state import InMemoryStateStore, award_key


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
    assert baseline.price_usd == 650.0
