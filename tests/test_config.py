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


def test_premium_cabin_max_multiplier_defaults_when_absent_from_fixture():
    # The fixture predates this field entirely -- must fall back to the
    # dataclass default (2.0), not raise or silently become 0/None.
    config = load_watchlist(FIXTURE_PATH)
    assert config.awards.premium_cabin_max_multiplier == 2.0


def test_transfer_bonus_pct_defaults_to_empty_dict_when_absent_from_fixture():
    config = load_watchlist(FIXTURE_PATH)
    assert config.awards.transfer_bonus_pct == {}
    assert config.awards.bonus_pct("aeroplan") == 0.0  # no entry -> 0.0, not a crash


def test_bonus_pct_reads_configured_value(tmp_path):
    fixture_with_bonus = tmp_path / "with_bonus.yaml"
    text = FIXTURE_PATH.read_text()
    # Inject a transfer_bonus_pct block right after cpp_floors' "default" line.
    text = text.replace(
        "    default: 1.4\n",
        "    default: 1.4\n  transfer_bonus_pct:\n    aeroplan: 0.25\n",
    )
    fixture_with_bonus.write_text(text)

    config = load_watchlist(fixture_with_bonus)
    assert config.awards.bonus_pct("aeroplan") == 0.25
    assert config.awards.bonus_pct("united") == 0.0  # not listed -> default 0.0


def test_eligible_programs_loads_from_top_level_key():
    config = load_watchlist(FIXTURE_PATH)
    assert config.eligible_programs == ["aeroplan", "united", "flyingblue"]


def test_eligible_programs_defaults_to_none_when_absent(tmp_path):
    # None means unrestricted -- must not silently become an empty (i.e.
    # everything-rejected) list when the key is simply omitted.
    fixture_no_eligible = tmp_path / "no_eligible_programs.yaml"
    text = FIXTURE_PATH.read_text()
    lines = [line for line in text.splitlines() if not line.startswith("eligible_programs:")]
    fixture_no_eligible.write_text("\n".join(lines))

    config = load_watchlist(fixture_no_eligible)
    assert config.eligible_programs is None


def test_route_without_origins_override_falls_back_to_top_level():
    config = load_watchlist(FIXTURE_PATH)
    italy = next(r for r in config.routes if r.name == "DC → Italy")
    assert italy.origins is None


def test_route_with_origins_override_is_parsed():
    config = load_watchlist(FIXTURE_PATH)
    europe = next(r for r in config.routes if r.name == "DC → Europe (broad)")
    assert europe.origins == ["IAD", "BWI"]


def test_date_window_offsets_from_today():
    config = load_watchlist(FIXTURE_PATH)
    italy = next(r for r in config.routes if r.name == "DC → Italy")

    today = datetime.date(2026, 7, 16)
    start, end = italy.date_window.to_dates(today=today)
    assert start == today + datetime.timedelta(days=30)
    assert end == today + datetime.timedelta(days=330)


# Upper bound on a route's date_window WIDTH (end_offset - start_offset),
# not its raw end_offset -- see test_real_watchlist_date_windows_stay_within_safe_bounds's
# docstring for why width is the right invariant, not absolute distance from
# today. Not the tuned value itself, which is expected to move within this
# range as routes get tuned. Just wide enough to rule out a regression back
# toward something like the original 330-day-wide window, not so tight that
# routine tuning breaks this test for reasons unrelated to a real bug.
_MAX_SAFE_WINDOW_WIDTH_DAYS = 200


def test_real_watchlist_date_windows_stay_within_safe_bounds():
    """Regression: the real watchlist.yaml's DC -> Italy route used to have
    start_offset: 0(-ish), end_offset: 330 -- a ~330-day-WIDE rolling window.
    Combined with prod having no per-run alert cap at the time, that wide
    window against an empty dedup table produced a real 73-alert flood in one
    invoke, because a wide window surfaces many distinct candidate
    dates/awards at once.

    Checked as WIDTH (end_offset - start_offset), not raw end_offset: a
    narrow (~2-week) window anchored far in the future -- e.g. DC -> Italy's
    2027-07-07 bracket, offsets ~347-361 -- has the same small
    number-of-candidates profile as a narrow window anchored 30 days out. It
    was never the distance from today that caused the flood, it was the
    SPAN. Raw end_offset alone would incorrectly flag a legitimate
    far-future-but-narrow bracket as unsafe.

    Unlike the other tests in this file (which deliberately use the fixture
    so routine tuning of the live config doesn't break them), this one
    intentionally targets the LIVE watchlist.yaml at the repo root -- but as
    a bounds check, not an exact-value match, so tuning within a safe range
    keeps passing; only a regression toward an extreme WIDE window like the
    original 330-day-wide one fails it."""
    config = load_watchlist()  # real watchlist.yaml, not the fixture
    for route in config.active_routes():
        width = route.date_window.end_offset - route.date_window.start_offset
        assert width <= _MAX_SAFE_WINDOW_WIDTH_DAYS, (
            f"{route.name}'s date_window width "
            f"({width} days, offsets {route.date_window.start_offset}-{route.date_window.end_offset}) "
            f"exceeds the safe bound of {_MAX_SAFE_WINDOW_WIDTH_DAYS} -- a wide window against an "
            "empty dedup table is what caused a 73-alert flood in one real invoke."
        )


def test_2027_italy_window_brackets_target_date_as_relative_offset():
    """DC -> Italy's date_window is meant to bracket 2027-07-07 with a
    ~2-week window -- but per DateWindow's shape (start_offset/end_offset
    ints only, no date fields), it MUST be expressed as an offset from
    today, not a hardcoded absolute date that would silently go stale as
    real time passes. Proven two ways: (1) with "today" pinned to when this
    route was configured, the realized window actually brackets the
    intended target date; (2) shifting "today" shifts the realized window by
    the same amount -- a hardcoded absolute date could never do that."""
    config = load_watchlist()  # real watchlist.yaml, not the fixture
    italy = next(r for r in config.active_routes() if r.name == "DC → Italy")

    today = datetime.date(2026, 7, 18)
    start, end = italy.date_window.to_dates(today=today)
    assert start <= datetime.date(2027, 7, 7) <= end
    assert (end - start).days <= 21  # "roughly 2 weeks", not a whole month

    # Relative-offset proof: a hardcoded absolute date would NOT shift when
    # "today" shifts -- an offset-based one shifts by exactly the same delta.
    shifted_today = today + datetime.timedelta(days=100)
    shifted_start, shifted_end = italy.date_window.to_dates(today=shifted_today)
    assert shifted_start == start + datetime.timedelta(days=100)
    assert shifted_end == end + datetime.timedelta(days=100)


def test_real_watchlist_thresholds_match_validated_values():
    """Regression: min_trip_value_usd=250 and cpp_floor=2.0 (both eligible
    per-program entries and the 'default' fallback) were validated 2026-07-18
    against real IAD-Europe economy data via scripts/dry_run.py's
    --cpp-floor/--min-trip-value CLI overrides (thousands of real
    candidates; median real trip value ~$300-306, median real CPP
    ~0.8-0.83cpp) before being committed here as the real config -- unlike
    the CLI overrides (in-memory only, never touched this file), this test
    targets the LIVE watchlist.yaml at the repo root specifically to catch a
    regression back to the old, unvalidated 400/2.5 numbers, or a partial
    edit that updates 'default' but misses a per-program override (which
    would silently keep that program on the old floor, since cpp_floor()
    prefers a per-program entry over 'default')."""
    config = load_watchlist()  # real watchlist.yaml, not the fixture
    assert config.awards.min_trip_value_usd == 250

    # Every program actually reachable via eligible_programs must clear the
    # SAME validated floor -- not just 'default', since a per-program entry
    # left on the old value would silently override it for that program.
    assert config.eligible_programs is not None
    reachable_programs_with_explicit_floors = set(config.eligible_programs) & set(config.awards.cpp_floors)
    assert reachable_programs_with_explicit_floors, "expected at least one eligible program to have an explicit cpp_floor entry"
    for program in reachable_programs_with_explicit_floors:
        assert config.awards.cpp_floor(program) == 2.0, f"{program}'s cpp_floor was not updated to the validated 2.0"

    assert config.awards.cpp_floors["default"] == 2.0

    # aadvantage is deliberately left untouched (American isn't in
    # eligible_programs, so this entry is inert) -- confirms the update was
    # scoped to reachable programs + default, not an indiscriminate
    # find-replace across the whole cpp_floors dict.
    assert config.awards.cpp_floors.get("aadvantage") == 1.5


def test_real_watchlist_both_active_routes_track_economy_and_premium_cabins():
    """Regression: business/first were re-added 2026-07 alongside economy
    (not in place of it) on both active routes, now that
    premium_cabin_max_multiplier exists as a free sanity check -- catches a
    partial edit that widens one route's cabins but not the other's."""
    config = load_watchlist()  # real watchlist.yaml, not the fixture
    for route in config.active_routes():
        assert set(route.cabins) == {"economy", "business", "first"}, (
            f"{route.name}'s cabins are {route.cabins}, expected economy+business+first"
        )


def test_real_watchlist_premium_cabin_max_multiplier_is_configured():
    config = load_watchlist()  # real watchlist.yaml, not the fixture
    assert config.awards.premium_cabin_max_multiplier == 2.0


def test_real_watchlist_transfer_bonus_pct_covers_every_eligible_program():
    """Regression: transfer_bonus_pct is manually maintained -- every real
    eligible_programs entry should have an explicit (even if 0.0) entry, so a
    missing program isn't silently indistinguishable from "definitely no
    bonus" vs "nobody has checked yet"."""
    config = load_watchlist()  # real watchlist.yaml, not the fixture
    assert config.eligible_programs is not None
    for program in config.eligible_programs:
        assert program in config.awards.transfer_bonus_pct, f"{program} has no explicit transfer_bonus_pct entry"


def test_real_watchlist_virginatlantic_transfer_bonus_reflects_confirmed_promo():
    """Regression: virginatlantic has a real, confirmed-active Amex MR 30%
    transfer bonus (2026-07-19 through 2026-07-31) -- must be 0.3, not the
    unresearched-default 0.0 every other program currently sits at. This test
    targets the LIVE watchlist.yaml specifically so it fails loudly if the
    bonus is accidentally reset to 0.0 before the real expiry date, or if a
    find-replace ever touches every entry indiscriminately instead of just
    this one confirmed program."""
    config = load_watchlist()  # real watchlist.yaml, not the fixture
    assert config.awards.bonus_pct("virginatlantic") == 0.3

    # Every other eligible program is deliberately still 0.0 -- unresearched,
    # not confirmed zero, just not yet checked. Not asserting a specific
    # reason, just that this one confirmed promo didn't silently spread.
    for program in config.eligible_programs:
        if program == "virginatlantic":
            continue
        assert config.awards.bonus_pct(program) == 0.0, f"{program} unexpectedly has a nonzero transfer_bonus_pct"


def test_real_watchlist_europe_broad_route_has_no_origins_override():
    """Regression: DC -> Europe (broad) used to override origins to
    [IAD, BWI]. Removed 2026-07-19 -- a pre-flight cost check ahead of the
    first real digest run found scripts/.dry_run_state.json holds ZERO
    cached baselines for BWI-anything (never queried by anything, ever), so
    a run including it would be fully cache-cold across all 8 destinations,
    with SerpApi cost potentially doubling versus IAD alone. IAD-only is now
    the permanent decision, not a cost-driven deferral -- business/first were
    added alongside economy afterward, tripling per-route cabin fan-out,
    which only strengthens the case rather than weakening it -- see
    watchlist.yaml's comment on this route. Targets the LIVE watchlist.yaml
    specifically (not the fixture) to catch a regression back to re-adding
    BWI."""
    config = load_watchlist()  # real watchlist.yaml, not the fixture
    europe = next(r for r in config.routes if r.name == "DC → Europe (broad)")
    assert europe.origins is None  # falls back to the top-level list, no override
    assert config.origins == ["IAD"]  # ...which is IAD-only, no BWI anywhere
