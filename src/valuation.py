"""Is this award worth interrupting the owner's day for?

Two-stage gate (see .claude/skills/deal-valuation):
  1. Prefilter: a cabin the owner tracks for this route. There is no
     per-item saver flag to check -- Cached Search is called without
     `include_filtered`, so every hit the poller sees is already
     saver-equivalent by construction (see providers/seats_aero.py).
  2. Value gate: once a comparable cash price exists, require the
     effective cents-per-point to clear the program's floor *and* the
     absolute trip value to clear min_trip_value_usd.

As of v1.1, poller.py wires a real `comparable_cash_usd` in from the cached
cash baseline (see src/cash.py), so stage 2 actually gates alerts.
`comparable_cash_usd=None` still degrades gracefully to prefilter-only
(stage 2 skipped) -- e.g. a baseline lookup failure, or a route with no
cash data yet -- rather than blocking the whole award pipeline on cash
being available.

This module also owns the second, independent trigger from deal-valuation:
`is_cash_price_drop()`, a standalone cash-fare-drop alert unrelated to any
specific award redemption.

`taxes_usd` is an explicit argument rather than always read off
`AwardAvailability.taxes_usd` because the poller calls this twice: once on
the raw Cached Search hit (real taxes, when the program reports them) and
again after Get Trips (the more authoritative figure, which can differ) --
see poller.py. It can be None: some programs (Qatar, Turkish, Singapore)
don't report taxes at all, and a genuinely unknown tax is never treated as
$0 here -- that would silently inflate the effective CPP.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.config import AwardConfig, CashConfig
from src.providers.seats_aero import AwardAvailability
from src.state import Baseline


@dataclass(frozen=True)
class Verdict:
    fire: bool
    reason: str
    headline: str


def passes_award_prefilter(award: AwardAvailability, wanted_cabins: set[str]) -> bool:
    return award.cabin in wanted_cabins


def compute_effective_cpp(comparable_cash_usd: float, taxes_fees_usd: float, miles: int) -> float:
    if miles <= 0:
        return 0.0
    return (comparable_cash_usd - taxes_fees_usd) / miles * 100


def is_high_value(
    award: AwardAvailability,
    config: AwardConfig,
    wanted_cabins: set[str],
    comparable_cash_usd: float | None = None,
    taxes_usd: float | None = None,
) -> Verdict:
    if not passes_award_prefilter(award, wanted_cabins):
        return Verdict(False, "cabin not tracked", "")

    if comparable_cash_usd is None:
        headline = f"{award.miles:,} {award.program} pts (no cash comparison yet)"
        return Verdict(True, "saver-equivalent availability in a tracked cabin", headline)

    if taxes_usd is None:
        return Verdict(
            False, f"taxes unknown for {award.program} (not reported by this program); can't verify effective CPP", ""
        )

    trip_value = comparable_cash_usd - taxes_usd
    if trip_value < config.min_trip_value_usd:
        return Verdict(False, f"trip value ${trip_value:,.0f} below floor ${config.min_trip_value_usd:,.0f}", "")

    cpp = compute_effective_cpp(comparable_cash_usd, taxes_usd, award.miles)
    floor = config.cpp_floor(award.program)
    if cpp < floor:
        return Verdict(False, f"{cpp:.1f}cpp below {award.program} floor {floor:.1f}cpp", "")

    headline = f"{cpp:.1f}¢/pt vs ${comparable_cash_usd:,.0f} cash"
    return Verdict(True, f"{cpp:.1f}cpp >= {award.program} floor {floor:.1f}cpp", headline)


def is_cash_price_drop(current_price_usd: float, baseline: Baseline, config: CashConfig) -> Verdict:
    """The second, independent trigger from deal-valuation: a standalone
    cash fare drop, unrelated to any award redemption.

    `baseline` must be the baseline as it stood BEFORE this observation
    (src/cash.py's CashBaselineUpdate.previous) -- comparing against a
    baseline already updated with the current price would compare a price
    against itself (blended into the EMA) and could never show a real drop.
    Never called on a route's first-ever observation: the caller (poller.py)
    only invokes this when `previous is not None`, so a route is always
    seeded silently first, per the skill's "never alert before a baseline
    exists" rule.

    Compares against baseline.ema_usd (the "typical" recent price), not
    trailing_min_usd (the all-time low) -- a drop below the historic minimum
    would almost never fire, whereas a drop below the recent average is the
    actual "is this unusually cheap right now" signal. trailing_min_usd is
    still tracked and surfaced in the alert for context.
    """
    if baseline.ema_usd <= 0:
        return Verdict(False, "baseline is zero/invalid, can't compute a drop", "")

    drop_abs = baseline.ema_usd - current_price_usd
    drop_pct = drop_abs / baseline.ema_usd

    if drop_pct >= config.min_drop_pct or drop_abs >= config.min_drop_abs_usd:
        kind = "possible mistake fare" if drop_pct >= config.mistake_fare_pct else "cash price drop"
        headline = f"${current_price_usd:,.0f} vs ${baseline.ema_usd:,.0f} baseline (-{drop_pct * 100:.0f}%)"
        return Verdict(True, kind, headline)

    return Verdict(
        False,
        f"drop {drop_pct * 100:.0f}% (${drop_abs:,.0f}) below thresholds "
        f"({config.min_drop_pct * 100:.0f}%/${config.min_drop_abs_usd:,.0f})",
        "",
    )
