"""StateStore: dedup (mandatory before every alert send) + cash baselines.

See .claude/skills/deal-valuation for the key design and TTL rationale.
"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass
from typing import Protocol

import boto3

from src.providers.seats_aero import AwardAvailability


def award_key(a: AwardAvailability) -> str:
    """Bucket miles so small fluctuations don't re-fire, but a materially
    better price (crossing into a lower bucket) is allowed to re-alert."""
    miles_bucket = a.miles // 5000 * 5000
    return f"award:{a.origin}-{a.destination}:{a.date.isoformat()}:{a.cabin}:{a.program}:{miles_bucket}"


@dataclass(frozen=True)
class Baseline:
    price_usd: float
    updated_at: datetime.datetime


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
        self._baselines[route_key] = Baseline(
            price_usd=price, updated_at=datetime.datetime.now(datetime.timezone.utc)
        )


class DynamoStateStore:
    """Production StateStore. See infra/dynamodb.tf for the table shape
    (alerts table has a TTL attribute; baselines table does not)."""

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
            price_usd=float(item["price_usd"]),
            updated_at=datetime.datetime.fromisoformat(item["updated_at"]),
        )

    def update_baseline(self, route_key: str, price: float) -> None:
        self._baselines.put_item(
            Item={
                "route_key": route_key,
                "price_usd": price,
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        )
