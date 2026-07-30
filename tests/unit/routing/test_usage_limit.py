"""Tests for routing.usage_limit.detect_usage_limit."""

from datetime import datetime, timedelta, timezone

from routing.usage_limit import detect_usage_limit

NOW = datetime(2026, 7, 30, 20, 0, 0, tzinfo=timezone.utc)


def test_returns_none_for_empty_or_non_usage_limit():
    assert detect_usage_limit(None, now=NOW) is None
    assert detect_usage_limit("", now=NOW) is None
    assert detect_usage_limit("HTTP 502 from upstream: bad gateway", now=NOW) is None


def test_transient_rate_limit_is_not_a_usage_limit():
    # A per-minute rate limit recovers on its own and must keep alerting, even
    # when it phrases its recovery with a reset marker.
    assert detect_usage_limit("429 Too Many Requests: rate limit exceeded", now=NOW) is None
    assert detect_usage_limit("rate limit will reset in 60 seconds", now=NOW) is None
    assert (
        detect_usage_limit(
            "429 rate limit exceeded; your limit will reset at 2026-07-30 22:00:00", now=NOW
        )
        is None
    )


def test_reset_phrase_without_rate_limit_is_a_usage_limit():
    # A bare reset phrase with no "rate limit" qualifier is treated as a usage
    # limit (default window, since no named period is present).
    limit = detect_usage_limit("Your limit will reset at 2026-07-30 22:45:10", now=NOW)
    assert limit is not None
    assert limit.reset_at == datetime(2026, 7, 30, 22, 45, 10, tzinfo=timezone.utc)


def test_explicit_reset_timestamp_wins_over_window():
    detail = (
        '{"error":{"code":"1308","message":"Usage limit reached for 5 hour. '
        'Your limit will reset at 2026-07-30 22:45:10"}}'
    )
    limit = detect_usage_limit(detail, now=NOW)
    assert limit is not None
    assert limit.reset_at == datetime(2026, 7, 30, 22, 45, 10, tzinfo=timezone.utc)
    # The named window still supplies a human label.
    assert limit.window == "5 hour"


def test_explicit_reset_timestamp_with_zone_normalized_to_utc():
    detail = "usage limit reached. limit will reset at 2026-07-30T22:45:10+02:00"
    limit = detect_usage_limit(detail, now=NOW)
    assert limit is not None
    assert limit.reset_at == datetime(2026, 7, 30, 20, 45, 10, tzinfo=timezone.utc)


def test_weekly_window_without_timestamp():
    detail = (
        "you (1a1a11a) have reached your weekly usage limit, upgrade for higher "
        "limits: or add extra usage: (ref: 514ea676-b534-4bb3-92ee-6f2198bb645d)"
    )
    limit = detect_usage_limit(detail, now=NOW)
    assert limit is not None
    assert limit.window == "weekly"
    assert limit.reset_at == NOW + timedelta(weeks=1)


def test_hour_window_count_without_timestamp():
    limit = detect_usage_limit("Usage limit reached for 5 hours.", now=NOW)
    assert limit is not None
    assert limit.reset_at == NOW + timedelta(hours=5)
    assert limit.window == "5 hour"


def test_usage_limit_without_window_uses_default():
    limit = detect_usage_limit("Your subscription limit has been reached.", now=NOW)
    assert limit is not None
    assert limit.window == "unspecified"
    assert limit.reset_at == NOW + timedelta(hours=1)


def test_past_explicit_timestamp_falls_back_to_named_window():
    # Clock skew or a wrong zone: a reset "in the past" is ignored so we don't
    # mute for only the min floor; the named window drives suppression instead.
    detail = "Usage limit reached for 5 hour. Your limit will reset at 2020-01-01 00:00:00"
    limit = detect_usage_limit(detail, now=NOW)
    assert limit is not None
    assert limit.reset_at == NOW + timedelta(hours=5)


def test_reset_clamped_to_max_window():
    # A monthly window (~31d) is capped to the 8-day suppression ceiling.
    limit = detect_usage_limit("reached your monthly usage limit", now=NOW)
    assert limit is not None
    assert limit.reset_at == NOW + timedelta(days=8)


def test_reset_floored_to_min_window():
    soon = (NOW + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
    limit = detect_usage_limit(f"usage limit reached; limit will reset at {soon}", now=NOW)
    assert limit is not None
    # A near-immediate reset is floored so re-alerts don't thrash.
    assert limit.reset_at == NOW + timedelta(minutes=5)


def test_today_is_not_parsed_as_a_daily_window():
    # "today" embeds "day" but must not read as a daily window (\b guards it).
    limit = detect_usage_limit("usage limit reached, try again today", now=NOW)
    assert limit is not None
    assert limit.window == "unspecified"
    assert limit.reset_at == NOW + timedelta(hours=1)
