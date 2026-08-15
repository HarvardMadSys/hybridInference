"""Tests for the api_logs JSONL exporter's date window and SQL."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from ops.db.export_logs import build_select_sql, parse_bound, previous_iso_week


def test_parse_bound_date_is_midnight_utc() -> None:
    assert parse_bound("2026-08-03") == datetime(2026, 8, 3, tzinfo=timezone.utc)


def test_parse_bound_naive_datetime_is_utc() -> None:
    assert parse_bound("2026-08-03T12:30:00") == datetime(2026, 8, 3, 12, 30, tzinfo=timezone.utc)


def test_parse_bound_aware_datetime_converts_to_utc() -> None:
    assert parse_bound("2026-08-03T08:00:00-04:00") == datetime(
        2026, 8, 3, 12, 0, tzinfo=timezone.utc
    )


def test_parse_bound_zulu_suffix() -> None:
    assert parse_bound("2026-08-03T00:00:00Z") == datetime(2026, 8, 3, tzinfo=timezone.utc)


def test_parse_bound_empty_is_none() -> None:
    assert parse_bound(None) is None
    assert parse_bound("") is None
    assert parse_bound("   ") is None


def test_parse_bound_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_bound("last-week")


def test_previous_iso_week_from_a_saturday() -> None:
    start, end = previous_iso_week(date(2026, 8, 15))
    assert (start, end) == (date(2026, 8, 3), date(2026, 8, 9))


def test_previous_iso_week_from_a_monday_is_the_week_just_closed() -> None:
    start, end = previous_iso_week(date(2026, 8, 17))
    assert (start, end) == (date(2026, 8, 10), date(2026, 8, 16))


def test_build_select_sql_unbounded_exports_the_whole_table() -> None:
    sql, args = build_select_sql(None, None)
    assert sql == "SELECT * FROM api_logs ORDER BY id"
    assert args == []


def test_build_select_sql_applies_inclusive_since_and_exclusive_until() -> None:
    since = parse_bound("2026-08-03")
    until = parse_bound("2026-08-10")
    sql, args = build_select_sql(since, until)
    assert sql == "SELECT * FROM api_logs WHERE timestamp >= $1 AND timestamp < $2 ORDER BY id"
    assert args == [since, until]


def test_build_select_sql_since_only() -> None:
    since = parse_bound("2026-08-03")
    sql, args = build_select_sql(since, None)
    assert sql == "SELECT * FROM api_logs WHERE timestamp >= $1 ORDER BY id"
    assert args == [since]
