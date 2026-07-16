"""Loads watchlist.yaml into typed config objects.

Routes, cabins, date windows, CPP floors, and alert thresholds are
config-as-code (see CLAUDE.md) — adding a route or tuning a threshold is a
watchlist.yaml edit, never a code change.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
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


@dataclass(frozen=True)
class AwardConfig:
    min_trip_value_usd: float
    cpp_floors: dict[str, float]

    def cpp_floor(self, program: str) -> float:
        return self.cpp_floors.get(program, self.cpp_floors.get("default", 1.4))


@dataclass(frozen=True)
class CashConfig:
    min_drop_pct: float
    min_drop_abs_usd: float
    mistake_fare_pct: float


@dataclass(frozen=True)
class ScheduleConfig:
    award_cached_minutes: int
    cash_baseline_minutes: int


@dataclass(frozen=True)
class AlertConfig:
    dedup_ttl_days: int


@dataclass(frozen=True)
class WatchlistConfig:
    origins: list[str]
    routes: list[RouteConfig]
    awards: AwardConfig
    cash: CashConfig
    schedule: ScheduleConfig
    alerts: AlertConfig

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
    )
