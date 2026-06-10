"""Tests for RouteWise provider quota snapshots."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from routing.routewise.candidates import QuotaSource
from routing.routewise.quota import ProviderQuotaSnapshotStore
from serving.schemas_admin import ProviderQuotaResult, ProviderQuotaUsage


def _source() -> QuotaSource:
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
    source = _source()

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
    source = _source()

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
    source = _source()

    await store.refresh_once([source])

    assert store.get(source) is None
    assert store.consume(source) is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exhausted_snapshot_cannot_be_consumed() -> None:
    async def fetch_chutes() -> list[ProviderQuotaResult]:
        return [_result(used=99.0, limit=100.0)]

    store = ProviderQuotaSnapshotStore(fetchers={"chutes": fetch_chutes})
    source = _source()

    await store.refresh_once([source])

    assert store.consume(source) is True
    assert store.consume(source) is False
    assert store.get(source).remaining == 0
