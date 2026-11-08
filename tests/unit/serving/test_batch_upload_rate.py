"""The hourly upload limit counts every upload opened, failed ones too (L24).

A failed upload's row is deleted, so the rows alone count only the uploads
that survived, and a caller whose uploads always fail was never limited. The
file store also counts the uploads it opened in the last hour, per user, and
the limit takes the larger count. The pool here answers like a database that
holds no upload of the user at all.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from serving.storage import batch_file_store
from serving.storage.batch_file_store import BatchFileQuotaExceeded, BatchFileStore


class EmptyRows:
    """A connection whose user has stored nothing: every failed upload left no row."""

    @contextlib.asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql: str, *args: Any) -> str:
        return "INSERT 0 1"

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        return {"stored": 0, "recent": 0}


class Pool:
    @contextlib.asynccontextmanager
    async def acquire(self):
        yield EmptyRows()


async def _open(files: BatchFileStore, user: str = "alice", limit: int = 3) -> str:
    return await files.create_upload(
        user_id=user,
        filename="in.jsonl",
        purpose="batch",
        max_total_bytes=10_000,
        max_uploads_per_hour=limit,
    )


async def test_uploads_count_once_opened_whether_or_not_they_survive(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(batch_file_store.time, "monotonic", lambda: clock[0])
    files = BatchFileStore(Pool())

    for _ in range(3):
        await _open(files)
    with pytest.raises(BatchFileQuotaExceeded) as refused:
        await _open(files)
    assert (refused.value.kind, refused.value.current) == ("uploads_per_hour", 3)
    # Another user has their own count.
    await _open(files, user="bob")
    # An hour on, the oldest have left the window.
    clock[0] += batch_file_store._UPLOAD_RATE_WINDOW + 1
    await _open(files)


async def test_users_with_nothing_recent_are_forgotten(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(batch_file_store.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(batch_file_store, "_ATTEMPTS_TRACKED", 2)
    files = BatchFileStore(Pool())
    for user in ("a", "b", "c"):
        await _open(files, user=user)
    clock[0] += batch_file_store._UPLOAD_RATE_WINDOW + 1

    await _open(files, user="d")

    assert set(files._attempts) == {"d"}
