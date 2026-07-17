from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from src.config import AwardConfig
from src.providers.seats_aero import AwardAvailability

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


@pytest.fixture(autouse=True)
def _not_in_lambda(monkeypatch):
    """secrets.py branches on AWS_LAMBDA_FUNCTION_NAME to decide env-var vs.
    SSM resolution -- ensure it's unset by default so tests exercise the
    local/dev path unless a test explicitly opts into the Lambda path."""
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)


@pytest.fixture
def award_config() -> AwardConfig:
    return AwardConfig(
        min_trip_value_usd=1500,
        cpp_floors={"aeroplan": 1.5, "united": 1.3, "default": 1.4},
    )


@pytest.fixture
def saver_business_award() -> AwardAvailability:
    return AwardAvailability(
        origin="IAD",
        destination="FCO",
        date=datetime.date(2026, 5, 14),
        program="aeroplan",
        cabin="business",
        miles=88000,
        taxes_usd=180.0,
        airlines=["AC"],
        direct=True,
        seats=2,
        availability_id="aeroplan-iad-fco-2026-05-14",
    )
