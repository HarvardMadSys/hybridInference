"""How the log store runs duplicate-account scans: shared, and one at a time.

A scan reads the whole window's request log on a general-pool connection, so
repeated clicks and a second administrator must not stack scans: a window's
scan is shared for half a minute by every request for that window, whatever
its ``min_score`` and ``limit``, and a process runs one scan at a time, with
the wait for another one counted against the scan's budget.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from serving.storage import postgres_log
from serving.storage.postgres_log import PostgresLogStore


class _Pool:
    """A pool whose connections are a placeholder: the scan itself is faked."""

    def acquire(self) -> _Pool:
        return self

    async def __aenter__(self) -> str:
        return "conn"

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Window:
    """A scanned window that reports the bounds it was asked for."""

    def __init__(self, days: int) -> None:
        self.days = days

    def report(self, *, min_score: int, limit: int) -> dict[str, Any]:
        return {"days": self.days, "min_score": min_score, "limit": limit}


async def test_a_window_is_scanned_once_for_any_bounds_and_scans_never_overlap(monkeypatch):
    scanned: list[int] = []
    running = peak = 0

    async def gather(conn: Any, *, days: int, budget_sec: float) -> _Window:
        nonlocal running, peak
        assert conn == "conn"
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        scanned.append(days)
        return _Window(days)

    monkeypatch.setattr(postgres_log, "gather_duplicate_scan", gather)
    store = PostgresLogStore(_Pool())

    first, stricter, other = await asyncio.gather(
        store.scan_duplicate_accounts(days=14, min_score=5, limit=100),
        store.scan_duplicate_accounts(days=14, min_score=9, limit=3),
        store.scan_duplicate_accounts(days=30, min_score=5, limit=100),
    )
    # Changing the bounds afterwards reuses the window's scan.
    again = await store.scan_duplicate_accounts(days=14, min_score=7, limit=1)

    assert sorted(scanned) == [14, 30]
    assert peak == 1
    assert first == {"days": 14, "min_score": 5, "limit": 100}
    assert stricter == {"days": 14, "min_score": 9, "limit": 3}
    assert other == {"days": 30, "min_score": 5, "limit": 100}
    assert again == {"days": 14, "min_score": 7, "limit": 1}


async def test_a_failed_scan_is_not_kept(monkeypatch):
    attempts = 0

    async def gather(conn: Any, *, days: int, budget_sec: float) -> _Window:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError
        return _Window(days)

    monkeypatch.setattr(postgres_log, "gather_duplicate_scan", gather)
    store = PostgresLogStore(_Pool())

    with pytest.raises(TimeoutError):
        await store.scan_duplicate_accounts(days=14, min_score=5, limit=100)

    assert await store.scan_duplicate_accounts(days=14, min_score=5, limit=100) == {
        "days": 14,
        "min_score": 5,
        "limit": 100,
    }
    assert attempts == 2


async def test_a_scan_waiting_for_another_gives_up_within_its_budget(monkeypatch):
    scanned: list[int] = []

    async def gather(conn: Any, *, days: int, budget_sec: float) -> _Window:
        scanned.append(days)
        return _Window(days)

    monkeypatch.setattr(postgres_log, "gather_duplicate_scan", gather)
    monkeypatch.setattr(postgres_log, "SCAN_BUDGET_SEC", 0.05)
    store = PostgresLogStore(_Pool())

    await store._duplicate_scan_lock.acquire()  # another scan, running long
    try:
        with pytest.raises(TimeoutError):
            await store.scan_duplicate_accounts(days=14, min_score=5, limit=100)
    finally:
        store._duplicate_scan_lock.release()

    assert scanned == []
    # The lock is free again, and giving up was not kept as the answer.
    assert (await store.scan_duplicate_accounts(days=14, min_score=5, limit=100))["days"] == 14
    assert scanned == [14]


async def test_the_wait_for_another_scan_spends_the_budget(monkeypatch):
    budgets: list[float] = []

    async def gather(conn: Any, *, days: int, budget_sec: float) -> _Window:
        budgets.append(budget_sec)
        return _Window(days)

    monkeypatch.setattr(postgres_log, "gather_duplicate_scan", gather)
    monkeypatch.setattr(postgres_log, "SCAN_BUDGET_SEC", 10.0)
    store = PostgresLogStore(_Pool())

    await store._duplicate_scan_lock.acquire()
    waiting = asyncio.ensure_future(store.scan_duplicate_accounts(days=14, min_score=5, limit=100))
    await asyncio.sleep(0.05)
    store._duplicate_scan_lock.release()
    await waiting

    [budget] = budgets
    assert budget <= 10.0 - 0.05
