"""How the log store runs duplicate-account scans: shared, and one at a time.

A scan reads the whole window's request log on a general-pool connection, so
repeated clicks and a second administrator must not stack scans: identical
requests share one computation for half a minute, and a process runs one scan
at a time whatever its bounds.
"""

from __future__ import annotations

import asyncio
from typing import Any

from serving.storage import postgres_log
from serving.storage.postgres_log import PostgresLogStore


class _Pool:
    """A pool whose connections are a placeholder: the scan itself is faked."""

    acquired = 0

    def acquire(self) -> _Pool:
        return self

    async def __aenter__(self) -> str:
        _Pool.acquired += 1
        return "conn"

    async def __aexit__(self, *exc: object) -> bool:
        return False


async def test_identical_scans_share_one_computation_and_scans_never_overlap(monkeypatch):
    calls: list[tuple[int, int, int]] = []
    running = peak = 0

    async def scan(conn: Any, *, days: int, min_score: int, limit: int) -> dict[str, Any]:
        nonlocal running, peak
        assert conn == "conn"
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        calls.append((days, min_score, limit))
        return {"days": days, "min_score": min_score}

    monkeypatch.setattr(postgres_log, "scan_duplicate_accounts", scan)
    store = PostgresLogStore(_Pool())

    first, again, other = await asyncio.gather(
        store.scan_duplicate_accounts(days=14, min_score=5, limit=100),
        store.scan_duplicate_accounts(days=14, min_score=5, limit=100),
        store.scan_duplicate_accounts(days=30, min_score=5, limit=100),
    )
    cached = await store.scan_duplicate_accounts(days=14, min_score=5, limit=100)

    assert sorted(calls) == [(14, 5, 100), (30, 5, 100)]
    assert peak == 1
    assert first is again is cached
    assert other == {"days": 30, "min_score": 5}


async def test_a_failed_scan_is_not_kept(monkeypatch):
    attempts = 0

    async def scan(conn: Any, **bounds: int) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError
        return {"ok": True}

    monkeypatch.setattr(postgres_log, "scan_duplicate_accounts", scan)
    store = PostgresLogStore(_Pool())

    try:
        await store.scan_duplicate_accounts(days=14, min_score=5, limit=100)
    except TimeoutError:
        pass
    else:  # pragma: no cover - the first attempt must fail
        raise AssertionError("the first scan should have failed")

    assert await store.scan_duplicate_accounts(days=14, min_score=5, limit=100) == {"ok": True}
    assert attempts == 2
