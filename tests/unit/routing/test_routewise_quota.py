"""Tests for RouteWise quota: policies, pools, and the provider snapshot store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from routing.routewise.candidates import QuotaPolicy, QuotaSource
from routing.routewise.quota import ProviderQuotaSnapshotStore, QuotaPool
from serving.schemas_admin import ProviderQuotaResult, ProviderQuotaUsage


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
class TestQuotaPool:
    def _source(self) -> QuotaSource:
        return QuotaSource(provider="chutes", usage_label="Daily requests", unit="requests")

    def test_not_ready_before_first_snapshot(self):
        store = _StubSnapshotStore()
        pool = QuotaPool(store, self._source(), policy=_policy(5000))
        assert pool.ready is False
        assert pool.remaining == 0
        assert pool.used_fraction == 1.0

    def test_reads_snapshot_state(self):
        store = _StubSnapshotStore()
        source = self._source()
        store.snapshots[source] = _StubSnapshot(limit=5000, remaining=1200, used_fraction=0.76)
        pool = QuotaPool(store, source, policy=_policy(5000))
        assert pool.ready is True
        assert pool.remaining == 1200
        assert pool.used_fraction == pytest.approx(0.76)
        assert pool.limit == 5000

    def test_consume_delegates_to_store(self):
        store = _StubSnapshotStore()
        source = self._source()
        store.snapshots[source] = _StubSnapshot(limit=5000, remaining=10, used_fraction=0.99)
        pool = QuotaPool(store, source, policy=_policy(5000))
        assert pool.consume() is True
        assert store.consumed == [source]
        store.consume_result = False
        assert pool.consume() is False

    def test_provider_limit_wins_and_mismatch_warns_once(self, caplog):
        store = _StubSnapshotStore()
        source = self._source()
        store.snapshots[source] = _StubSnapshot(limit=4000, remaining=100, used_fraction=0.97)
        pool = QuotaPool(store, source, policy=_policy(5000))
        with caplog.at_level("WARNING"):
            assert pool.limit == 4000
            assert pool.limit == 4000
        assert sum("provider reports limit" in r.message for r in caplog.records) == 1


# ---------------------------------------------------------------------------
# ProviderQuotaSnapshotStore (refresh / optimistic increments)
# ---------------------------------------------------------------------------


def _store_source() -> QuotaSource:
    return QuotaSource(provider="chutes", usage_label="Daily requests", unit="requests")


def _result(
    *,
    used: float,
    limit: float,
    label: str = "Daily requests",
    unit: str = "requests",
) -> ProviderQuotaResult:
    return ProviderQuotaResult(
        name="chutes",
        display_name="Chutes",
        key_configured=True,
        key_masked="***",
        fetched_at=datetime.now(timezone.utc),
        ok=True,
        error=None,
        usages=[
            ProviderQuotaUsage(
                label=label,
                used=used,
                limit=limit,
                unit=unit,
                reset_at=None,
            )
        ],
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refresh_get_and_consume_snapshot() -> None:
    async def fetch_chutes() -> list[ProviderQuotaResult]:
        return [_result(used=10.0, limit=100.0)]

    store = ProviderQuotaSnapshotStore(fetchers={"chutes": fetch_chutes})
    source = _store_source()

    assert store.get(source) is None

    await store.refresh_once([source])
    snapshot = store.get(source)
    assert snapshot is not None
    assert snapshot.used == 10.0
    assert snapshot.limit == 100.0
    assert snapshot.remaining == 90
    assert snapshot.used_fraction == pytest.approx(0.10)

    assert store.consume(source) is True
    after_consume = store.get(source)
    assert after_consume is not None
    assert after_consume.remaining == 89
    assert after_consume.used_fraction == pytest.approx(0.11)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refresh_resets_local_increment_to_provider_truth() -> None:
    calls = 0

    async def fetch_chutes() -> list[ProviderQuotaResult]:
        nonlocal calls
        calls += 1
        return [_result(used=10.0 if calls == 1 else 20.0, limit=100.0)]

    store = ProviderQuotaSnapshotStore(fetchers={"chutes": fetch_chutes})
    source = _store_source()

    await store.refresh_once([source])
    assert store.consume(source) is True
    assert store.get(source).remaining == 89

    await store.refresh_once([source])
    snapshot = store.get(source)
    assert snapshot is not None
    assert snapshot.remaining == 80
    assert snapshot.local_increment == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_matching_usage_leaves_snapshot_absent() -> None:
    async def fetch_chutes() -> list[ProviderQuotaResult]:
        return [_result(used=5.0, limit=10.0, label="Monthly", unit="USD")]

    store = ProviderQuotaSnapshotStore(fetchers={"chutes": fetch_chutes})
    source = _store_source()

    await store.refresh_once([source])

    assert store.get(source) is None
    assert store.consume(source) is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exhausted_snapshot_cannot_be_consumed() -> None:
    async def fetch_chutes() -> list[ProviderQuotaResult]:
        return [_result(used=99.0, limit=100.0)]

    store = ProviderQuotaSnapshotStore(fetchers={"chutes": fetch_chutes})
    source = _store_source()

    await store.refresh_once([source])

    assert store.consume(source) is True
    assert store.consume(source) is False
    assert store.get(source).remaining == 0
