"""Unit tests for AdminAnalyticsResponse schema."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from serving.schemas_admin import (
    AdminAnalyticsResponse,
    AnalyticsBreakdownEntry,
    AnalyticsModelUserEntry,
    AnalyticsModelUsers,
    AnalyticsUserEntry,
    SparklineBucket,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_admin_analytics_response_full():
    resp = AdminAnalyticsResponse(
        period="day",
        active_users=47,
        avg_turns=12.5,
        avg_user_turns=6.25,
        sparkline=[SparklineBucket(start_time=_now(), request_count=10)],
        top_users=[
            AnalyticsUserEntry(email="alice@example.com", user_id="u1", requests=200, fraction=0.5)
        ],
        by_model=[AnalyticsBreakdownEntry(name="claude-sonnet-4-6", requests=200, fraction=0.5)],
        by_provider=[AnalyticsBreakdownEntry(name="anthropic", requests=200, fraction=0.5)],
        by_model_top_users=[
            AnalyticsModelUsers(
                model="claude-sonnet-4-6",
                requests=200,
                tokens=15000,
                users=[
                    AnalyticsModelUserEntry(
                        email="alice@example.com", user_id="u1", requests=200, tokens=15000
                    )
                ],
            )
        ],
        generated_at=_now(),
    )
    assert resp.period == "day"
    assert resp.active_users == 47
    assert resp.avg_turns == 12.5
    assert resp.avg_user_turns == 6.25
    assert resp.by_model_top_users[0].model == "claude-sonnet-4-6"
    assert resp.by_model_top_users[0].tokens == 15000
    assert resp.by_model_top_users[0].users[0].email == "alice@example.com"


def test_admin_analytics_response_turn_averages_default_to_none():
    # Averages are optional so a period with no chat requests still validates.
    resp = AdminAnalyticsResponse(
        period="day",
        active_users=0,
        sparkline=[],
        top_users=[],
        by_model=[],
        by_provider=[],
        by_model_top_users=[],
        generated_at=_now(),
    )
    assert resp.avg_turns is None
    assert resp.avg_user_turns is None


def test_admin_analytics_response_invalid_period():
    with pytest.raises(ValidationError):
        AdminAnalyticsResponse(
            period="invalid",
            active_users=0,
            sparkline=[],
            top_users=[],
            by_model=[],
            by_provider=[],
            by_model_top_users=[],
            generated_at=_now(),
        )
