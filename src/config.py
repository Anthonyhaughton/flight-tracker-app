"""Loads watchlist.yaml into typed config objects.

Routes, cabins, date windows, CPP floors, and alert thresholds are
config-as-code (see CLAUDE.md) — adding a route or tuning a threshold is a
watchlist.yaml edit, never a code change.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "watchlist.yaml"


@dataclass(frozen=True)
class DateWindow:
    start_offset: int
    end_offset: int

    def to_dates(self, today: datetime.date | None = None) -> tuple[datetime.date, datetime.date]:
        today = today or datetime.date.today()
        return (
            today + datetime.timedelta(days=self.start_offset),
            today + datetime.timedelta(days=self.end_offset),
        )


@dataclass(frozen=True)
class RouteConfig:
    name: str
    destinations: list[str]
    cabins: list[str]
    date_window: DateWindow
    active: bool = True
    # Per-route override of the top-level origins list. None (the default)
    # means "use the top-level origins list" -- most routes don't need their
    # own set of departure airports, so this only exists for the routes that
    # actually differ (e.g. a route that excludes DCA).
    origins: list[str] | None = None


@dataclass(frozen=True)
class AwardConfig:
    min_trip_value_usd: float
    cpp_floors: dict[str, float]
    # Free (no cash lookup) sanity check on a business/first candidate: reject
    # if its miles cost exceeds this multiplier times economy's miles cost on
    # the SAME seats.aero record (AwardAvailability.economy_miles) -- see
    # src/valuation.py's passes_award_prefilter. Only applies to business/
    # first; economy candidates are unaffected. Defaulted so existing
    # AwardConfig(...) call sites (mostly tests) that predate this field
    # don't break.
    premium_cabin_max_multiplier: float = 2.0
    # Per-program transfer bonus, as a fraction (0.25 = 25%), e.g. a
    # promotional Amex MR/Chase UR -> program transfer bonus. Manually
    # maintained in watchlist.yaml -- no automated fetching of any kind.
    # Defaults to 0.0 (no bonus) for any program not listed. Purely
    # informational: surfaced alongside the real CPP number in alerts/digest
    # (see notify/discord.py, notify/telegram.py), never fed into any
    # gating/threshold decision.
    transfer_bonus_pct: dict[str, float] = field(default_factory=dict)

    def cpp_floor(self, program: str) -> float:
        return self.cpp_floors.get(program, self.cpp_floors.get("default", 1.4))

    def bonus_pct(self, program: str) -> float:
        return self.transfer_bonus_pct.get(program, 0.0)


@dataclass(frozen=True)
class CashConfig:
    min_drop_pct: float
    min_drop_abs_usd: float
    mistake_fare_pct: float
    # Independent, history-free trigger: ANY one-way price under this fires a
    # "possible mistake fare" alert regardless of baseline/EMA history -- in
    # addition to (not instead of) the relative baseline-drop trigger above,
    # since it must be able to fire on a route's very first observation,
    # before any baseline exists at all. See src/valuation.py's
    # is_cash_below_mistake_fare_ceiling. Defaulted so existing CashConfig(...)
    # call sites that predate this field don't break.
    mistake_fare_ceiling_usd: float = 200.0


@dataclass(frozen=True)
class ScheduleConfig:
    award_cached_minutes: int
    cash_baseline_minutes: int


@dataclass(frozen=True)
class AlertConfig:
    dedup_ttl_days: int
    # Total NEW deals alerted in a single poller invocation -- kept as
    # documentation/reference (should equal economy_reserved_slots +
    # premium_reserved_slots below) but is NOT itself what's enforced; see
    # those two fields for the real per-tier gating. Independent of dedup
    # (which only stops the SAME deal re-alerting on a LATER run) -- this
    # guards against a wide date window or a newly widened/added route
    # surfacing many qualifying candidates against an empty dedup table in
    # one run. See src/poller.py's run()/poll_route().
    max_alerts_per_run: int = 8
    # Reserved, hard-partitioned slices of max_alerts_per_run -- NOT a shared
    # pool. Real 2026-07-19 production-like measurements (two consecutive
    # real polls) showed business/first structurally out-competing economy
    # for a shared cap every time (16 for 16 real sends across both polls
    # were business/first, zero economy), because a premium redemption's
    # bigger cash-vs-miles gap clears both the CPP floor and
    # min_trip_value_usd more easily than economy ever will -- see
    # .claude/skills/deal-valuation. Economy gets a GUARANTEED share of the
    # budget: an exhausted tier's unused slots are never borrowed by the
    # other tier, in either direction -- see src/valuation.py's
    # is_premium_cabin and src/poller.py's reserved-slot cap check. Applied
    # identically to src/digest.py's top-5 ranking (there, premium is capped
    # at premium_reserved_slots per list; economy is never actually bound by
    # its own 6, since a top-5 list can't hold 6 anyway -- see digest.py).
    economy_reserved_slots: int = 6
    premium_reserved_slots: int = 2


@dataclass(frozen=True)
class WatchlistConfig:
    origins: list[str]
    routes: list[RouteConfig]
    awards: AwardConfig
    cash: CashConfig
    schedule: ScheduleConfig
    alerts: AlertConfig
    notifier: str = "discord"  # "discord" (default) or "telegram" -- swap without code changes
    # Award "Source" (program) keys eligible to alert on -- e.g. the seats.aero
    # source keys reachable via the owner's actual transfer partnerships. None
    # (the default) means unrestricted, matching pre-eligible_programs
    # behavior. Re-verify periodically: transfer partnerships get added and
    # dropped over time, see the comment above eligible_programs in
    # watchlist.yaml. Applied as an early prefilter, before dedup/cap/cash
    # lookups -- see src/valuation.py's passes_award_prefilter.
    eligible_programs: list[str] | None = None

    def active_routes(self) -> list[RouteConfig]:
        return [r for r in self.routes if r.active]


def load_watchlist(path: str | Path = DEFAULT_WATCHLIST_PATH) -> WatchlistConfig:
    raw = yaml.safe_load(Path(path).read_text())
    routes = [
        RouteConfig(
            name=r["name"],
            destinations=r["destinations"],
            cabins=r["cabins"],
            date_window=DateWindow(**r["date_window"]),
            active=r.get("active", True),
            origins=r.get("origins"),
        )
        for r in raw["routes"]
    ]
    return WatchlistConfig(
        origins=raw["origins"],
        routes=routes,
        awards=AwardConfig(**raw["awards"]),
        cash=CashConfig(**raw["cash"]),
        schedule=ScheduleConfig(**raw["schedule"]),
        alerts=AlertConfig(**raw["alerts"]),
        notifier=raw.get("notifier", "discord"),
        eligible_programs=raw.get("eligible_programs"),
    )
