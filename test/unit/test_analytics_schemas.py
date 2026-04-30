"""Unit tests for AdminAnalyticsResponse schema."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from serving.schemas_admin import (
    AdminAnalyticsResponse,
    AnalyticsBreakdownEntry,
    AnalyticsUserEntry,
    SparklineBucket,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_sparkline_bucket_valid():
    b = SparklineBucket(start_time=_now(), request_count=42)
    assert b.request_count == 42


def test_analytics_user_entry_fraction_range():
    e = AnalyticsUserEntry(
        email="alice@example.com",
        user_id="u1",
        requests=100,
        fraction=0.38,
    )
    assert e.fraction == pytest.approx(0.38)


def test_analytics_breakdown_entry_others():
    e = AnalyticsBreakdownEntry(name="others", requests=50, fraction=0.12)
    assert e.name == "others"


def test_admin_analytics_response_full():
    resp = AdminAnalyticsResponse(
        period="day",
        active_users=47,
        sparkline=[SparklineBucket(start_time=_now(), request_count=10)],
        top_users=[
            AnalyticsUserEntry(
                email="alice@example.com", user_id="u1", requests=200, fraction=0.5
            )
        ],
        by_model=[AnalyticsBreakdownEntry(name="claude-sonnet-4-6", requests=200, fraction=0.5)],
        by_provider=[AnalyticsBreakdownEntry(name="anthropic", requests=200, fraction=0.5)],
        generated_at=_now(),
    )
    assert resp.period == "day"
    assert resp.active_users == 47


def test_admin_analytics_response_invalid_period():
    with pytest.raises(ValidationError):
        AdminAnalyticsResponse(
            period="invalid",
            active_users=0,
            sparkline=[],
            top_users=[],
            by_model=[],
            by_provider=[],
            generated_at=_now(),
        )
