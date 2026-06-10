"""Tests for RouteWise snapshot quota pools and route-level quota policies."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from routing.routewise.candidates import QuotaPolicy, QuotaSource
from routing.routewise.quota import SnapshotQuotaPool


def _policy(limit: int) -> QuotaPolicy:
    return QuotaPolicy.from_raw({"limit": limit}, context="test.quota")


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

    def test_window_is_no_longer_a_policy_key(self):
        """Window/reset semantics are provider-side; the quota truth source

        (``quota_source``) reports them. A leftover ``window:`` block fails
        at boot instead of being silently ignored.
        """
        with pytest.raises(ValueError, match="unknown keys"):
            QuotaPolicy.from_raw({"limit": 10, "window": "daily"}, context="t")


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
        pool = SnapshotQuotaPool(store, self._source(), policy=_policy(5000))
        assert pool.ready is False
        assert pool.remaining == 0
        assert pool.used_fraction == 1.0

    def test_reads_snapshot_state(self):
        store = _StubSnapshotStore()
        source = self._source()
        store.snapshots[source] = _StubSnapshot(limit=5000, remaining=1200, used_fraction=0.76)
        pool = SnapshotQuotaPool(store, source, policy=_policy(5000))
        assert pool.ready is True
        assert pool.remaining == 1200
        assert pool.used_fraction == pytest.approx(0.76)
        assert pool.limit == 5000

    def test_consume_delegates_to_store(self):
        store = _StubSnapshotStore()
        source = self._source()
        store.snapshots[source] = _StubSnapshot(limit=5000, remaining=10, used_fraction=0.99)
        pool = SnapshotQuotaPool(store, source, policy=_policy(5000))
        assert pool.consume() is True
        assert store.consumed == [source]
        store.consume_result = False
        assert pool.consume() is False

    def test_provider_limit_wins_and_mismatch_warns_once(self, caplog):
        store = _StubSnapshotStore()
        source = self._source()
        store.snapshots[source] = _StubSnapshot(limit=4000, remaining=100, used_fraction=0.97)
        pool = SnapshotQuotaPool(store, source, policy=_policy(5000))
        with caplog.at_level("WARNING"):
            assert pool.limit == 4000
            assert pool.limit == 4000
        assert sum("provider reports limit" in r.message for r in caplog.records) == 1

    def test_percent_based_pool(self):
        """Percent-only usage APIs (e.g. MiniMax token-plan remains) map to a

        limit-100 pool: used/limit are percentage points and used_fraction is
        exact.
        """
        store = _StubSnapshotStore()
        source = QuotaSource(provider="minimax", usage_label="general (interval)", unit="%")
        store.snapshots[source] = _StubSnapshot(limit=100, remaining=98, used_fraction=0.02)
        pool = SnapshotQuotaPool(store, source, policy=_policy(100))
        assert pool.ready is True
        assert pool.remaining == 98
        assert pool.used_fraction == pytest.approx(0.02)
