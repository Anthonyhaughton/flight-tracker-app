"""StateStore: dedup (mandatory before every alert send) + cash baselines.

See .claude/skills/deal-valuation for the key design and TTL rationale, and
.claude/skills/flight-cash-price-monitor for the baseline-tracking model.
"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import boto3

from src.providers.cash.base import CashFare
from src.providers.seats_aero import AwardAvailability

# EMA smoothing factor for Baseline.ema_usd -- higher reacts faster to recent
# prices, lower is steadier. 0.3 is a moderate default (roughly a ~3-4
# observation half-life); tune here if it proves too twitchy/sluggish.
EMA_ALPHA = 0.3


def award_key(a: AwardAvailability) -> str:
    """Bucket miles so small fluctuations don't re-fire, but a materially
    better price (crossing into a lower bucket) is allowed to re-alert."""
    miles_bucket = a.miles // 5000 * 5000
    return f"award:{a.origin}-{a.destination}:{a.date.isoformat()}:{a.cabin}:{a.program}:{miles_bucket}"


def cash_key(f: CashFare) -> str:
    """Bucket price so tiny fluctuations don't re-fire, but a materially
    better price (crossing into a lower $50 bucket) is allowed to re-alert.
    Per .claude/skills/deal-valuation's dedup key design."""
    price_bucket = int(f.price_usd // 50 * 50)
    return f"cash:{f.origin}-{f.destination}:{f.date.isoformat()}:{f.cabin}:{price_bucket}"


def baseline_key(origin: str, destination: str, cabin: str, date: datetime.date) -> str:
    """route+cabin+date-bucket, per .claude/skills/flight-cash-price-monitor.
    Bucketed by ISO week (not exact date) so nearby travel dates within the
    same route/cabin share one cached baseline -- this is what bounds
    SerpApi call volume: at most one refresh per route+cabin+week per
    cash_baseline_minutes window, regardless of how many exact days within
    that week have qualifying award candidates. Tunable if a coarser/finer
    bucket proves better."""
    iso_year, iso_week, _ = date.isocalendar()
    return f"{origin}-{destination}:{cabin}:{iso_year}-W{iso_week:02d}"


@dataclass(frozen=True)
class Baseline:
    trailing_min_usd: float   # lowest price ever observed for this key
    ema_usd: float             # exponential moving average -- the "typical" price
    updated_at: datetime.datetime


def compute_updated_baseline(existing: Baseline | None, price: float, now: datetime.datetime) -> Baseline:
    """Shared math for both StateStore impls: seed both fields equal to the
    price on first observation (no history to smooth against yet); on every
    later observation, the trailing min can only decrease, and the EMA
    blends the new price in at EMA_ALPHA."""
    if existing is None:
        return Baseline(trailing_min_usd=price, ema_usd=price, updated_at=now)
    return Baseline(
        trailing_min_usd=min(existing.trailing_min_usd, price),
        ema_usd=EMA_ALPHA * price + (1 - EMA_ALPHA) * existing.ema_usd,
        updated_at=now,
    )


class StateStore(Protocol):
    def already_alerted(self, key: str) -> bool: ...
    def record_alert(self, key: str, ttl_seconds: int) -> None: ...
    def get_baseline(self, route_key: str) -> Baseline | None: ...
    def update_baseline(self, route_key: str, price: float) -> None: ...


class InMemoryStateStore:
    """Dependency-free StateStore for tests and local runs."""

    def __init__(self) -> None:
        self._alerts: dict[str, float] = {}
        self._baselines: dict[str, Baseline] = {}

    def already_alerted(self, key: str) -> bool:
        expires_at = self._alerts.get(key)
        if expires_at is None:
            return False
        if expires_at < time.time():
            del self._alerts[key]
            return False
        return True

    def record_alert(self, key: str, ttl_seconds: int) -> None:
        self._alerts[key] = time.time() + ttl_seconds

    def get_baseline(self, route_key: str) -> Baseline | None:
        return self._baselines.get(route_key)

    def update_baseline(self, route_key: str, price: float) -> None:
        existing = self._baselines.get(route_key)
        now = datetime.datetime.now(datetime.timezone.utc)
        self._baselines[route_key] = compute_updated_baseline(existing, price, now)


class DynamoStateStore:
    """Production StateStore. See infra/dynamodb.tf for the table shape
    (alerts table has a TTL attribute; baselines table does not).

    DynamoDB's resource API rejects native Python floats on writes (it
    requires Decimal for its Number type) -- prices are converted via
    Decimal(str(x)) rather than Decimal(x) to avoid binary-float rounding
    artifacts (e.g. Decimal(0.1) != Decimal("0.1"))."""

    def __init__(self, alerts_table: str, baselines_table: str, *, resource=None):
        self._dynamodb = resource or boto3.resource("dynamodb")
        self._alerts = self._dynamodb.Table(alerts_table)
        self._baselines = self._dynamodb.Table(baselines_table)

    def already_alerted(self, key: str) -> bool:
        response = self._alerts.get_item(Key={"dedup_key": key})
        return "Item" in response

    def record_alert(self, key: str, ttl_seconds: int) -> None:
        expires_at = int(time.time()) + ttl_seconds
        self._alerts.put_item(Item={"dedup_key": key, "expires_at": expires_at})

    def get_baseline(self, route_key: str) -> Baseline | None:
        response = self._baselines.get_item(Key={"route_key": route_key})
        item = response.get("Item")
        if not item:
            return None
        return Baseline(
            trailing_min_usd=float(item["trailing_min_usd"]),
            ema_usd=float(item["ema_usd"]),
            updated_at=datetime.datetime.fromisoformat(item["updated_at"]),
        )

    def update_baseline(self, route_key: str, price: float) -> None:
        existing = self.get_baseline(route_key)
        now = datetime.datetime.now(datetime.timezone.utc)
        new_baseline = compute_updated_baseline(existing, price, now)
        self._baselines.put_item(
            Item={
                "route_key": route_key,
                "trailing_min_usd": Decimal(str(new_baseline.trailing_min_usd)),
                "ema_usd": Decimal(str(new_baseline.ema_usd)),
                "updated_at": new_baseline.updated_at.isoformat(),
            }
        )
