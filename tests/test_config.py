from __future__ import annotations

import datetime
from pathlib import Path

from src.config import load_watchlist

# Fixture with its own stable values -- NOT the live watchlist.yaml at the
# repo root, which gets tuned for real routes/live testing and would break
# these hardcoded assertions for reasons unrelated to any real bug.
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "test_watchlist.yaml"


def test_loads_watchlist():
    config = load_watchlist(FIXTURE_PATH)
    assert config.origins == ["IAD", "DCA", "BWI"]
    assert len(config.routes) == 2


def test_active_routes_filters_inactive():
    config = load_watchlist(FIXTURE_PATH)
    active = config.active_routes()
    assert [r.name for r in active] == ["DC → Italy"]


def test_italy_route_targets_business_and_first_saver():
    config = load_watchlist(FIXTURE_PATH)
    italy = next(r for r in config.routes if r.name == "DC → Italy")
    assert italy.destinations == ["FCO", "MXP", "VCE"]
    assert italy.cabins == ["business", "first"]
    assert italy.active is True


def test_cpp_floor_falls_back_to_default():
    config = load_watchlist(FIXTURE_PATH)
    assert config.awards.cpp_floor("aeroplan") == 1.5
    assert config.awards.cpp_floor("some_unlisted_program") == config.awards.cpp_floors["default"]


def test_date_window_offsets_from_today():
    config = load_watchlist(FIXTURE_PATH)
    italy = next(r for r in config.routes if r.name == "DC → Italy")

    today = datetime.date(2026, 7, 16)
    start, end = italy.date_window.to_dates(today=today)
    assert start == today + datetime.timedelta(days=30)
    assert end == today + datetime.timedelta(days=330)


def test_real_watchlist_date_window_is_narrowed_for_safety():
    """Regression: the real watchlist.yaml's DC -> Italy route used to have
    end_offset: 330 (~11 months out). Combined with prod having no per-run
    alert cap at the time, that wide window against an empty dedup table
    produced a real 73-alert flood in one invoke. Unlike the other tests in
    this file (which deliberately use the fixture so routine tuning of the
    live config doesn't break them), this one intentionally targets the
    LIVE watchlist.yaml at the repo root -- it exists specifically to catch
    someone widening the window back out past the safe range without
    noticing the guardrail comment above date_window in that file."""
    config = load_watchlist()  # real watchlist.yaml, not the fixture
    italy = next(r for r in config.routes if r.name == "DC → Italy")
    assert italy.date_window.start_offset == 30
    assert italy.date_window.end_offset == 150
