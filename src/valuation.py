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
`comparable_cash_usd=None` -- a baseline lookup failure, a provider error,
or a route with no cash data yet (e.g. genuinely too far out for the cash
provider to have anything) -- always SKIPS, never fires. This was not
always true: an earlier version fell back to firing on cabin-match alone
when cash data was unavailable, on the reasoning that an award pipeline
shouldn't be blocked by a cash-side outage. That fallback direction was
retired 2026-07 as a real safety issue found in an architecture review: it
meant the cash pipeline's failure mode was MORE alerts, not fewer, on a
system whose explicit top priority is avoiding alert fatigue. There is no
route-level opt-out of this -- it applies everywhere, unconditionally.

This module also owns two independent cash-fare triggers from deal-valuation,
both unrelated to any specific award redemption: `is_cash_price_drop()` (a
relative drop below the recent baseline/EMA) and
`is_cash_below_mistake_fare_ceiling()` (an absolute price ceiling, fires
regardless of baseline history -- including on a route's very first
observation, unlike the drop trigger).

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
from typing import Protocol, TypeVar

from src.config import AwardConfig, CashConfig
from src.providers.seats_aero import AwardAvailability
from src.state import Baseline


@dataclass(frozen=True)
class Verdict:
    fire: bool
    reason: str
    headline: str


def passes_award_prefilter(
    award: AwardAvailability,
    wanted_cabins: set[str],
    eligible_programs: set[str] | None = None,
    premium_cabin_max_multiplier: float | None = None,
) -> bool:
    """eligible_programs is the set of seats.aero source keys reachable via
    the owner's actual transfer partnerships (see watchlist.yaml's
    eligible_programs). None means unrestricted -- pre-eligible_programs
    behavior, and what every caller that doesn't care about this axis gets
    by default. A candidate whose program isn't eligible is rejected here,
    before a cash lookup or Get Trips call is ever spent on it.

    premium_cabin_max_multiplier (watchlist.yaml's awards.
    premium_cabin_max_multiplier, None means unrestricted -- same convention
    as eligible_programs) is a FREE sanity check, spent before any cash
    lookup: a business/first candidate is rejected if its own miles cost
    exceeds this multiplier times economy's miles cost on the SAME seats.aero
    record (award.economy_miles) -- e.g. a business fare costing >2x what
    economy costs on the identical record is a bad redemption on its face,
    not worth a paid cash-provider call to confirm. Only applies to cabin in
    {"business", "first"}; economy candidates are never subject to this
    check. When economy_miles is unresolvable for the record (None or <= 0
    -- economy genuinely wasn't available on it, or the field was absent),
    the ratio can't be verified, so the candidate is rejected rather than let
    through unchecked -- same "unknown is never assumed safe" rule as
    taxes_usd=None elsewhere in this module."""
    if award.cabin not in wanted_cabins:
        return False
    if eligible_programs is not None and award.program not in eligible_programs:
        return False
    if premium_cabin_max_multiplier is not None and award.cabin in ("business", "first"):
        if award.economy_miles is None or award.economy_miles <= 0:
            return False
        if award.miles > premium_cabin_max_multiplier * award.economy_miles:
            return False
    return True


def compute_effective_cpp(comparable_cash_usd: float, taxes_fees_usd: float, miles: int) -> float:
    if miles <= 0:
        return 0.0
    return (comparable_cash_usd - taxes_fees_usd) / miles * 100


def compute_transfer_bonus_effective_miles(miles: int, transfer_bonus_pct: float) -> float:
    """Informational only -- the real-currency-equivalent points cost of a
    transfer with an active bonus (e.g. a +25% Amex MR -> program transfer
    promo needs fewer actual MR points for the same award). Never feeds into
    compute_effective_cpp/is_high_value or any other gating decision -- see
    watchlist.yaml's transfer_bonus_pct comment and AwardConfig.bonus_pct.
    `transfer_bonus_pct` is a fraction (0.25 = 25%), matching this project's
    other *_pct config fields (e.g. CashConfig.min_drop_pct)."""
    return miles / (1 + transfer_bonus_pct)


def group_key(award: AwardAvailability) -> tuple:
    """(origin, destination, cabin, program, year, month) -- candidates
    sharing this key represent "the same deal on a different date within
    the same calendar month" for select_group_winners()'s purposes. Scoped
    to calendar MONTH, not the whole date window: two dates a month or more
    apart are genuinely different real trip options (different vacation
    windows), not near-duplicates of one deal -- collapsing an entire
    ~150-day window down to a single winner would be too aggressive and
    would silently hide real, distinct flexibility. See .claude/skills/
    deal-valuation's winner-selection spec."""
    return (award.origin, award.destination, award.cabin, award.program, award.date.year, award.date.month)


class _HasAwardAndCpp(Protocol):
    award: AwardAvailability
    cpp: float


_T = TypeVar("_T", bound=_HasAwardAndCpp)


def select_group_winners(candidates: list[_T]) -> list[tuple[_T, list]]:
    """Groups `candidates` by group_key() and returns ONLY the single
    highest-cpp candidate per group, paired with every OTHER date in that
    same group (sorted ascending, excluding the winner's own date) -- every
    other candidate is dropped entirely by simply not being included in the
    return value, so a caller that only acts on this function's output
    naturally never sends/caps/confirms them as anything but grouped out.

    Works on anything exposing `.award` (AwardAvailability) and `.cpp`
    (float, always the cheap first-pass/weekly-bucket estimate -- never a
    fresh or exact-date-confirmed lookup, so calling this costs nothing new)
    -- `AwardFirstPassResult` for the real-time path (src/poller.py) and
    `DigestEntry` for the weekly digest (src/digest.py) both qualify, so the
    SAME grouping mechanism serves both call sites, never reimplemented
    per caller (see .claude/skills/avoiding-duplicate-implementations).

    Ties (equal cpp within a group) are broken by earliest date -- arbitrary
    but deterministic, so the same input always produces the same winner."""
    groups: dict[tuple, list[_T]] = {}
    for c in candidates:
        groups.setdefault(group_key(c.award), []).append(c)

    winners: list[tuple[_T, list]] = []
    for group in groups.values():
        winner = max(group, key=lambda c: (c.cpp, -c.award.date.toordinal()))
        other_dates = sorted(c.award.date for c in group if c.award.date != winner.award.date)
        winners.append((winner, other_dates))
    return winners


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
        return Verdict(False, "cash data unavailable, skipping rather than firing blind", "")

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


def is_cash_below_mistake_fare_ceiling(current_price_usd: float, config: CashConfig) -> Verdict:
    """A third, independent cash trigger, alongside is_cash_price_drop:
    ANY one-way price under config.mistake_fare_ceiling_usd is flagged as a
    possible mistake fare regardless of baseline/EMA history -- unlike
    is_cash_price_drop, this has no history requirement at all, so the
    caller (poller.py) may invoke it even on a route's very first-ever
    observation, before any baseline exists. One-way only, matching the rest
    of the pipeline's deliberate one-way-only cash pricing (see
    src/providers/cash/serpapi.py's module docstring) -- there is no
    round-trip variant of this check."""
    if current_price_usd >= config.mistake_fare_ceiling_usd:
        return Verdict(
            False,
            f"${current_price_usd:,.0f} at/above the ${config.mistake_fare_ceiling_usd:,.0f} mistake-fare ceiling",
            "",
        )
    headline = f"${current_price_usd:,.0f} one-way (mistake-fare ceiling ${config.mistake_fare_ceiling_usd:,.0f})"
    return Verdict(True, "possible mistake fare (absolute ceiling)", headline)


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
