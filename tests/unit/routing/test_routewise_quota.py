"""Tests for RouteWise quota pools and route-level quota policies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from routing.routewise.candidates import QuotaPolicy, QuotaSource, QuotaWindow
from routing.routewise.quota import LocalQuotaPool, SnapshotQuotaPool


def _daily_policy(limit: int, timezone: str = "UTC") -> QuotaPolicy:
    return QuotaPolicy.from_raw(
        {"limit": limit, "window": {"type": "daily", "timezone": timezone}},
        context="test.quota",
    )


def _rolling_policy(limit: int, duration: str) -> QuotaPolicy:
    return QuotaPolicy.from_raw(
        {"limit": limit, "window": {"type": "rolling", "duration": duration}},
        context="test.quota",
    )


@pytest.mark.unit
class TestQuotaWindowParsing:
    def test_default_window_is_daily_utc(self):
        window = QuotaWindow.from_raw(None, context="t")
        assert window.type == "daily"
        assert window.timezone == "UTC"

    def test_string_sugar_for_daily(self):
        window = QuotaWindow.from_raw("daily", context="t")
        assert window.type == "daily"

    def test_rolling_duration_strings(self):
        assert QuotaWindow.from_raw(
            {"type": "rolling", "duration": "5h"}, context="t"
        ).duration_sec == pytest.approx(5 * 3600.0)
        assert QuotaWindow.from_raw(
            {"type": "rolling", "duration": "30m"}, context="t"
        ).duration_sec == pytest.approx(1800.0)
        assert QuotaWindow.from_raw(
            {"type": "rolling", "duration": 90}, context="t"
        ).duration_sec == pytest.approx(90.0)

    def test_rolling_requires_duration(self):
        with pytest.raises(ValueError, match="requires a duration"):
            QuotaWindow.from_raw({"type": "rolling"}, context="t")

    def test_unknown_type_rejected(self):
        with pytest.raises(ValueError, match="must be 'daily' or 'rolling'"):
            QuotaWindow.from_raw({"type": "weekly"}, context="t")

    def test_daily_rejects_duration_key(self):
        with pytest.raises(ValueError, match="unknown keys"):
            QuotaWindow.from_raw({"type": "daily", "duration": "5h"}, context="t")

    def test_rolling_rejects_timezone_key(self):
        with pytest.raises(ValueError, match="unknown keys"):
            QuotaWindow.from_raw(
                {"type": "rolling", "duration": "5h", "timezone": "UTC"}, context="t"
            )

    def test_invalid_timezone_rejected(self):
        with pytest.raises(ValueError, match="IANA timezone"):
            QuotaWindow.from_raw({"type": "daily", "timezone": "Mars/Olympus"}, context="t")


@pytest.mark.unit
class TestQuotaPolicyParsing:
    def test_limit_required(self):
        with pytest.raises(ValueError, match="limit"):
            QuotaPolicy.from_raw({}, context="t")

    def test_limit_must_be_positive_int(self):
        with pytest.raises(ValueError, match="limit"):
            QuotaPolicy.from_raw({"limit": 0}, context="t")
        with pytest.raises(ValueError, match="limit"):
            QuotaPolicy.from_raw({"limit": "5000"}, context="t")

    def test_unknown_keys_rejected(self):
        with pytest.raises(ValueError, match="unknown keys"):
            QuotaPolicy.from_raw({"limit": 10, "monthly_fee": 20.0}, context="t")


@pytest.mark.unit
class TestLocalQuotaPoolDaily:
    def test_initial_remaining(self):
        pool = LocalQuotaPool(_daily_policy(5000))
        assert pool.ready is True
        assert pool.remaining == 5000

    def test_consume_reduces_remaining(self):
        pool = LocalQuotaPool(_daily_policy(1000))
        assert pool.consume() is True
        assert pool.remaining == 999
        assert pool.consume() is True
        assert pool.remaining == 998

    def test_consume_refused_at_limit(self):
        pool = LocalQuotaPool(_daily_policy(3))
        for _ in range(3):
            assert pool.consume() is True
        assert pool.consume() is False
        assert pool.remaining == 0

    def test_used_fraction_progresses(self):
        pool = LocalQuotaPool(_daily_policy(10))
        assert pool.used_fraction == 0.0
        for _ in range(5):
            pool.consume()
        assert pool.used_fraction == pytest.approx(0.5)
        for _ in range(5):
            pool.consume()
        assert pool.used_fraction == 1.0

    def test_daily_reset(self):
        pool = LocalQuotaPool(_daily_policy(100))
        for _ in range(60):
            pool.consume()
        assert pool.remaining == 40

        tomorrow = datetime.now(tz=ZoneInfo("UTC")) + timedelta(days=1)
        with patch("routing.routewise.quota.datetime") as mock_dt:
            mock_dt.now.return_value = tomorrow
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert pool.remaining == 100

    def test_consume_triggers_reset_check(self):
        pool = LocalQuotaPool(_daily_policy(500))
        for _ in range(300):
            pool.consume()
        assert pool.remaining == 200

        tomorrow = datetime.now(tz=ZoneInfo("UTC")) + timedelta(days=1)
        with patch("routing.routewise.quota.datetime") as mock_dt:
            mock_dt.now.return_value = tomorrow
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert pool.consume() is True
            # Reset happened, then consumed 1.
            assert pool.remaining == 499


@pytest.mark.unit
class TestLocalQuotaPoolRolling:
    def test_slots_free_as_window_slides(self):
        clock = {"now": 1_000.0}
        pool = LocalQuotaPool(
            _rolling_policy(2, "5h"),
            time_source=lambda: clock["now"],
        )
        assert pool.consume() is True
        clock["now"] += 3600.0
        assert pool.consume() is True
        assert pool.consume() is False
        assert pool.remaining == 0

        # First request leaves the 5h window; one slot frees up.
        clock["now"] = 1_000.0 + 5 * 3600.0 + 1.0
        assert pool.remaining == 1
        assert pool.used_fraction == pytest.approx(0.5)
        assert pool.consume() is True
        assert pool.consume() is False

    def test_full_window_expiry_restores_all_slots(self):
        clock = {"now": 0.0}
        pool = LocalQuotaPool(
            _rolling_policy(3, "30m"),
            time_source=lambda: clock["now"],
        )
        for _ in range(3):
            assert pool.consume() is True
        assert pool.remaining == 0
        clock["now"] += 1800.0 + 1.0
        assert pool.remaining == 3
        assert pool.used_fraction == 0.0


@dataclass
class _StubSnapshot:
    limit: float
    remaining: int
    used_fraction: float


class _StubSnapshotStore:
    def __init__(self) -> None:
        self.snapshots: dict[QuotaSource, _StubSnapshot] = {}
        self.consumed: list[QuotaSource] = []
        self.consume_result = True

    def get(self, source: QuotaSource) -> _StubSnapshot | None:
        return self.snapshots.get(source)

    def consume(self, source: QuotaSource) -> bool:
        self.consumed.append(source)
        return self.consume_result


@pytest.mark.unit
class TestSnapshotQuotaPool:
    def _source(self) -> QuotaSource:
        return QuotaSource(provider="chutes", usage_label="Daily requests", unit="requests")

    def test_not_ready_before_first_snapshot(self):
        store = _StubSnapshotStore()
        pool = SnapshotQuotaPool(store, self._source(), policy=_daily_policy(5000))
        assert pool.ready is False
        assert pool.remaining == 0
        assert pool.used_fraction == 1.0

    def test_reads_snapshot_state(self):
        store = _StubSnapshotStore()
        source = self._source()
        store.snapshots[source] = _StubSnapshot(limit=5000, remaining=1200, used_fraction=0.76)
        pool = SnapshotQuotaPool(store, source, policy=_daily_policy(5000))
        assert pool.ready is True
        assert pool.remaining == 1200
        assert pool.used_fraction == pytest.approx(0.76)
        assert pool.limit == 5000

    def test_consume_delegates_to_store(self):
        store = _StubSnapshotStore()
        source = self._source()
        store.snapshots[source] = _StubSnapshot(limit=5000, remaining=10, used_fraction=0.99)
        pool = SnapshotQuotaPool(store, source, policy=_daily_policy(5000))
        assert pool.consume() is True
        assert store.consumed == [source]
        store.consume_result = False
        assert pool.consume() is False

    def test_provider_limit_wins_and_mismatch_warns_once(self, caplog):
        store = _StubSnapshotStore()
        source = self._source()
        store.snapshots[source] = _StubSnapshot(limit=4000, remaining=100, used_fraction=0.97)
        pool = SnapshotQuotaPool(store, source, policy=_daily_policy(5000))
        with caplog.at_level("WARNING"):
            assert pool.limit == 4000
            assert pool.limit == 4000
        assert sum("provider reports limit" in r.message for r in caplog.records) == 1
