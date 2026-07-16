from __future__ import annotations

import datetime

from src.providers.seats_aero import AwardAvailability
from src.valuation import compute_effective_cpp, is_high_value, passes_award_prefilter


def test_prefilter_rejects_untracked_cabin(saver_business_award):
    assert passes_award_prefilter(saver_business_award, {"economy"}) is False


def test_prefilter_accepts_tracked_cabin(saver_business_award):
    assert passes_award_prefilter(saver_business_award, {"business", "first"}) is True


def test_compute_effective_cpp_screaming_deal():
    # 120k miles for a $6,000 business seat -> 5.0 cpp
    cpp = compute_effective_cpp(comparable_cash_usd=6000, taxes_fees_usd=0, miles=120_000)
    assert cpp == 5.0


def test_compute_effective_cpp_trap():
    # 120k miles for a $900 economy seat -> 0.75 cpp
    cpp = compute_effective_cpp(comparable_cash_usd=900, taxes_fees_usd=0, miles=120_000)
    assert cpp == 0.75


def test_compute_effective_cpp_zero_miles_is_zero():
    assert compute_effective_cpp(comparable_cash_usd=500, taxes_fees_usd=0, miles=0) == 0.0


def test_is_high_value_v1_0_fires_without_cash_data(saver_business_award, award_config):
    # v1.0 has no cash provider wired up: cabin match alone fires (results
    # are already saver-equivalent by construction -- no per-item flag).
    verdict = is_high_value(saver_business_award, award_config, {"business", "first"})
    assert verdict.fire is True
    assert "88,000" in verdict.headline


def test_is_high_value_skips_untracked_cabin(saver_business_award, award_config):
    verdict = is_high_value(saver_business_award, award_config, {"economy"})
    assert verdict.fire is False
    assert verdict.reason == "cabin not tracked"


def test_is_high_value_with_cash_fires_above_floor(saver_business_award, award_config):
    # 88,000 miles, $180 taxes, $5,900 cash -> (5900-180)/88000*100 = 6.5 cpp, above 1.5 floor
    verdict = is_high_value(
        saver_business_award, award_config, {"business", "first"}, comparable_cash_usd=5900, taxes_usd=180
    )
    assert verdict.fire is True
    assert "cpp" in verdict.reason


def test_is_high_value_with_cash_skips_below_floor(saver_business_award, award_config):
    # cheap cash comparable drives cpp below the aeroplan 1.5 floor
    verdict = is_high_value(
        saver_business_award, award_config, {"business", "first"}, comparable_cash_usd=1600, taxes_usd=180
    )
    assert verdict.fire is False
    assert "below" in verdict.reason


def test_is_high_value_with_cash_skips_below_min_trip_value(saver_business_award, award_config):
    # trip value = comparable_cash - taxes must clear min_trip_value_usd (1500)
    verdict = is_high_value(
        saver_business_award, award_config, {"business", "first"}, comparable_cash_usd=1000, taxes_usd=180
    )
    assert verdict.fire is False
    assert "trip value" in verdict.reason


def test_is_high_value_skips_when_taxes_unknown(saver_business_award, award_config):
    # taxes_usd=None (e.g. a Qatar/Turkish/Singapore hit) must never be
    # silently treated as $0 -- that would inflate the effective CPP and
    # could fire on a deal that isn't actually verified as good.
    verdict = is_high_value(
        saver_business_award, award_config, {"business", "first"}, comparable_cash_usd=5900, taxes_usd=None
    )
    assert verdict.fire is False
    assert "taxes unknown" in verdict.reason


def test_is_high_value_uses_per_program_floor(award_config):
    united_award = AwardAvailability(
        origin="IAD",
        destination="FCO",
        date=datetime.date(2026, 6, 1),
        program="united",
        cabin="business",
        miles=115_000,
        taxes_usd=56.0,
        airlines=["UA", "LH"],
        direct=True,
        seats=1,
        availability_id="united-iad-fco-2026-06-01",
    )
    # (2000-56)/115000*100 = 1.69 cpp, clears united's 1.3 floor but not aeroplan's 1.5
    verdict = is_high_value(
        united_award, award_config, {"business", "first"}, comparable_cash_usd=2000, taxes_usd=56
    )
    assert verdict.fire is True
