"""Is this award worth interrupting the owner's day for?

Two-stage gate (see .claude/skills/deal-valuation):
  1. Prefilter: a cabin the owner tracks for this route. There is no
     per-item saver flag to check -- Cached Search is called without
     `include_filtered`, so every hit the poller sees is already
     saver-equivalent by construction (see providers/seats_aero.py).
  2. Value gate: once a comparable cash price exists (v1.1+), require the
     effective cents-per-point to clear the program's floor *and* the
     absolute trip value to clear min_trip_value_usd.

v1.0 has no cash provider wired into the poller yet, so `comparable_cash_usd`
is always None there and stage 2 is skipped -- the prefilter alone is the
v1.0 gate. The full CPP math is implemented and tested now so v1.1 only has
to wire in a cash price, not write new valuation logic.

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

from src.config import AwardConfig
from src.providers.seats_aero import AwardAvailability


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
