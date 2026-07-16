from __future__ import annotations

from src.config import load_watchlist


def test_loads_real_watchlist():
    config = load_watchlist()
    assert config.origins == ["IAD", "DCA", "BWI"]
    assert len(config.routes) == 2


def test_active_routes_filters_inactive():
    config = load_watchlist()
    active = config.active_routes()
    assert [r.name for r in active] == ["DC → Italy"]


def test_italy_route_targets_business_and_first_saver():
    config = load_watchlist()
    italy = next(r for r in config.routes if r.name == "DC → Italy")
    assert italy.destinations == ["FCO", "MXP", "VCE"]
    assert italy.cabins == ["business", "first"]
    assert italy.active is True


def test_cpp_floor_falls_back_to_default():
    config = load_watchlist()
    assert config.awards.cpp_floor("aeroplan") == 1.5
    assert config.awards.cpp_floor("some_unlisted_program") == config.awards.cpp_floors["default"]


def test_date_window_offsets_from_today():
    config = load_watchlist()
    italy = next(r for r in config.routes if r.name == "DC → Italy")
    import datetime

    today = datetime.date(2026, 7, 16)
    start, end = italy.date_window.to_dates(today=today)
    assert start == today + datetime.timedelta(days=30)
    assert end == today + datetime.timedelta(days=330)
