from __future__ import annotations

import dataclasses
import datetime

from src.config import AwardConfig, CashConfig
from src.providers.seats_aero import AwardAvailability
from src.state import Baseline
from src.valuation import (
    compute_effective_cpp,
    is_cash_below_mistake_fare_ceiling,
    is_cash_price_drop,
    is_high_value,
    passes_award_prefilter,
)


def test_prefilter_rejects_untracked_cabin(saver_business_award):
    assert passes_award_prefilter(saver_business_award, {"economy"}) is False


def test_prefilter_accepts_tracked_cabin(saver_business_award):
    assert passes_award_prefilter(saver_business_award, {"business", "first"}) is True


def test_prefilter_accepts_eligible_program(saver_business_award):
    # saver_business_award.program == "aeroplan"
    assert passes_award_prefilter(saver_business_award, {"business", "first"}, {"aeroplan", "united"}) is True


def test_prefilter_rejects_ineligible_program(saver_business_award):
    assert passes_award_prefilter(saver_business_award, {"business", "first"}, {"united", "delta"}) is False


def test_prefilter_eligible_programs_none_means_unrestricted(saver_business_award):
    # None (the default/omitted case) must not filter on program at all --
    # pre-eligible_programs behavior, unchanged for callers that don't pass it.
    assert passes_award_prefilter(saver_business_award, {"business", "first"}) is True
    assert passes_award_prefilter(saver_business_award, {"business", "first"}, None) is True


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


def test_is_high_value_skips_without_cash_data(saver_business_award, award_config):
    """Regression: an earlier version fell back to firing on cabin-match
    alone when comparable_cash_usd was None (no cash provider, a lookup
    failure, or genuinely no data yet). That fallback direction was retired
    -- a real safety issue found in an architecture review, since it meant
    a cash-pipeline outage's failure mode was MORE alerts, not fewer, on a
    system whose top priority is avoiding alert fatigue. No resolved cash
    price must always skip, on every route, unconditionally -- there is no
    per-route opt-out."""
    verdict = is_high_value(saver_business_award, award_config, {"business", "first"})
    assert verdict.fire is False
    assert verdict.reason == "cash data unavailable, skipping rather than firing blind"


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


def test_is_high_value_economy_fare_clears_lowered_min_trip_value(saver_business_award):
    # Realistic IAD-Europe economy one-way ($650, matching the live SerpApi
    # economy fixture's ~$689-742 range) with otherwise-good CPP. The old
    # min_trip_value_usd=1500 (a business/first number) would reject this
    # outright regardless of CPP; the economy-appropriate 400 must not.
    economy_config = AwardConfig(min_trip_value_usd=400, cpp_floors={"default": 2.5})
    economy_award = dataclasses.replace(saver_business_award, cabin="economy", miles=20000)
    # trip value = 650 - 75 = 575 >= 400 floor; cpp = (650-75)/20000*100 = 2.875cpp >= 2.5 floor
    verdict = is_high_value(economy_award, economy_config, {"economy"}, comparable_cash_usd=650, taxes_usd=75)
    assert verdict.fire is True
    assert "trip value" not in verdict.reason


def test_is_high_value_economy_fare_still_rejected_by_old_business_floor():
    # Documents the bug the fix addresses: the SAME realistic economy fare
    # from the test above WOULD have been rejected purely on trip value
    # under the old 1500 business/first floor, even with identical
    # (otherwise-clearing) CPP -- proving 400 is what changed the outcome,
    # not the CPP math.
    old_business_config = AwardConfig(min_trip_value_usd=1500, cpp_floors={"default": 2.5})
    economy_award = AwardAvailability(
        origin="IAD", destination="FCO", date=datetime.date(2027, 7, 5), program="united", cabin="economy",
        miles=20000, taxes_usd=75.0, airlines=["UA"], direct=True, seats=4,
        availability_id="united-iad-fco-2027-07-05-economy",
    )
    verdict = is_high_value(economy_award, old_business_config, {"economy"}, comparable_cash_usd=650, taxes_usd=75)
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


# --- is_cash_price_drop: the second, independent trigger from deal-valuation ---

_CASH_CONFIG = CashConfig(min_drop_pct=0.20, min_drop_abs_usd=150, mistake_fare_pct=0.45)


def _baseline(ema_usd: float, trailing_min_usd: float | None = None) -> Baseline:
    return Baseline(
        trailing_min_usd=trailing_min_usd if trailing_min_usd is not None else ema_usd,
        ema_usd=ema_usd,
        updated_at=datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc),
    )


def test_is_cash_price_drop_fires_above_pct_threshold():
    # (1000-750)/1000 = 25% drop, clears the 20% floor
    verdict = is_cash_price_drop(750.0, _baseline(1000.0), _CASH_CONFIG)
    assert verdict.fire is True
    assert "cash price drop" in verdict.reason


def test_is_cash_price_drop_fires_on_abs_threshold_even_when_pct_is_small():
    # (2000-1850)/2000 = 7.5%, well under the 20% floor -- but the $150
    # absolute drop alone is enough per the "or" in the formula.
    verdict = is_cash_price_drop(1850.0, _baseline(2000.0), _CASH_CONFIG)
    assert verdict.fire is True


def test_is_cash_price_drop_does_not_fire_below_both_thresholds():
    verdict = is_cash_price_drop(1950.0, _baseline(2000.0), _CASH_CONFIG)  # $50 / 2.5%
    assert verdict.fire is False
    assert "below thresholds" in verdict.reason


def test_is_cash_price_drop_flags_extreme_drop_as_mistake_fare():
    # 60% drop clears the 45% mistake-fare threshold
    verdict = is_cash_price_drop(800.0, _baseline(2000.0), _CASH_CONFIG)
    assert verdict.fire is True
    assert verdict.reason == "possible mistake fare"


def test_is_cash_price_drop_normal_drop_not_flagged_as_mistake_fare():
    # 25% drop clears min_drop_pct but not the 45% mistake-fare bar
    verdict = is_cash_price_drop(1500.0, _baseline(2000.0), _CASH_CONFIG)
    assert verdict.fire is True
    assert verdict.reason == "cash price drop"


def test_is_cash_price_drop_handles_invalid_zero_baseline():
    verdict = is_cash_price_drop(500.0, _baseline(0.0), _CASH_CONFIG)
    assert verdict.fire is False
    assert "invalid" in verdict.reason


# --- is_cash_below_mistake_fare_ceiling: third, independent cash trigger,
# fires with NO baseline/history required at all ---


def test_is_cash_below_mistake_fare_ceiling_fires_under_ceiling():
    verdict = is_cash_below_mistake_fare_ceiling(180.0, _CASH_CONFIG)
    assert verdict.fire is True
    assert verdict.reason == "possible mistake fare (absolute ceiling)"
    assert "180" in verdict.headline


def test_is_cash_below_mistake_fare_ceiling_does_not_fire_at_ceiling():
    # _CASH_CONFIG.mistake_fare_ceiling_usd defaults to 200.0 -- exactly AT
    # the ceiling must not fire (only strictly under it).
    verdict = is_cash_below_mistake_fare_ceiling(200.0, _CASH_CONFIG)
    assert verdict.fire is False
    assert "at/above" in verdict.reason


def test_is_cash_below_mistake_fare_ceiling_does_not_fire_above_ceiling():
    verdict = is_cash_below_mistake_fare_ceiling(250.0, _CASH_CONFIG)
    assert verdict.fire is False


def test_is_cash_below_mistake_fare_ceiling_respects_configured_value():
    custom_config = dataclasses.replace(_CASH_CONFIG, mistake_fare_ceiling_usd=100.0)
    assert is_cash_below_mistake_fare_ceiling(150.0, custom_config).fire is False
    assert is_cash_below_mistake_fare_ceiling(90.0, custom_config).fire is True
