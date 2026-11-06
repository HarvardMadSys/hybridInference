"""``last_used_at`` is written at most once a minute per key, not on every request.

Every authenticated request used to fire an unconditional ``UPDATE api_keys
SET last_used_at = NOW()`` as an unbounded background task: row-lock
contention on a busy key, and a pile of tasks ahead of the auth lookups on the
shared pool while the database is slow. The write is now skipped when this
process scheduled one for the key within a minute, and the UPDATE itself skips
a row any process wrote within a minute.
"""

from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request

from serving.servers import auth
from serving.servers.auth import (
    LAST_USED_DEBOUNCE_SECONDS,
    touch_key_last_used,
    verify_api_key,
    verify_batch_management_identity,
)
from serving.storage.base import OperationalStore
from serving.storage.postgres_operational import PostgresOperationalStore


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    """The auth module's clock only: the event loop keeps the real one."""
    clock = _Clock()
    monkeypatch.setattr(auth, "time", types.SimpleNamespace(monotonic=clock))
    return clock


def _store() -> MagicMock:
    store = MagicMock(spec=OperationalStore)
    store.get_auth_context_with_spend = AsyncMock(
        return_value={
            "id": 1,
            "user_id": "user123",
            "quota_daily_cost_usd": 1000.0,
            "role": "free",
            "email": "test@example.com",
            "email_verified": True,
            "cost_usd": 0.0,
        }
    )
    store.update_key_last_used = AsyncMock()
    return store


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def _verify(store: MagicMock) -> dict:
    return await verify_api_key(
        request=MagicMock(spec=Request),
        authorization="Bearer hyi-debounce",
        x_on_behalf_of=None,
        op_store=store,
        log_store=MagicMock(),
    )


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")


@pytest.mark.asyncio
async def test_a_burst_of_requests_writes_last_used_once(clock):
    store = _store()

    for _ in range(50):
        await _verify(store)
        clock.now += 0.1
    await _settle()

    store.update_key_last_used.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_the_next_write_follows_once_the_window_has_passed(clock):
    store = _store()

    await _verify(store)
    clock.now += LAST_USED_DEBOUNCE_SECONDS - 1
    await _verify(store)
    clock.now += 1
    await _verify(store)
    await _settle()

    assert store.update_key_last_used.await_count == 2


@pytest.mark.asyncio
async def test_batch_management_shares_the_debounce(clock):
    store = _store()

    await _verify(store)
    await verify_batch_management_identity(
        request=MagicMock(spec=Request),
        authorization="Bearer hyi-debounce",
        x_on_behalf_of=None,
        op_store=store,
    )
    await _settle()

    store.update_key_last_used.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_keys_and_stores_are_debounced_apart(clock):
    first, second = _store(), _store()

    touch_key_last_used(first, 1)
    touch_key_last_used(first, 2)
    touch_key_last_used(second, 1)
    touch_key_last_used(first, 1)
    await _settle()

    assert [c.args for c in first.update_key_last_used.await_args_list] == [(1,), (2,)]
    second.update_key_last_used.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_remembered_keys_are_swept_once_expired(clock, monkeypatch):
    monkeypatch.setattr(auth, "_LAST_USED_SWEEP_AT", 3)
    store = _store()
    for key_id in (1, 2, 3):
        touch_key_last_used(store, key_id)
    clock.now += LAST_USED_DEBOUNCE_SECONDS
    touch_key_last_used(store, 4)
    await _settle()

    assert set(auth._last_used_written[store]) == {4}


@pytest.mark.asyncio
async def test_the_update_skips_a_row_written_within_the_window():
    conn = AsyncMock()
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)

    await PostgresOperationalStore(pool).update_key_last_used(9)

    [call] = conn.execute.await_args_list
    sql, key_id = call.args
    assert key_id == 9
    assert sql.startswith("UPDATE api_keys SET last_used_at = NOW() WHERE id = $1")
    assert "last_used_at IS NULL OR last_used_at < NOW() - INTERVAL '60 seconds'" in sql
