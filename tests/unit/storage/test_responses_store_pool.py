"""The Responses store and the dashboard's usage read never hang behind a busy general pool.

Both share the general pool with the log writers, batch work and rollups. A
call made while a request is served waits a bounded time for a connection and
then fails with ``DatabaseBusyError`` (the app's 503 with ``Retry-After``); the
hard-delete purge and the retention job, which no client waits on, still wait.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.servers.routers import user_routes
from serving.storage import pools, postgres_log, responses_store
from serving.storage.pools import DatabaseBusyError
from serving.storage.postgres_log import PostgresLogStore
from serving.storage.responses_store import ResponseStore

pytestmark = pytest.mark.asyncio


class _Pool:
    """A pool whose one connection is free after *free_after* seconds (never, for None)."""

    def __init__(self, free_after: float | None) -> None:
        self.free_after = free_after
        self.conn = MagicMock()
        self.conn.execute = AsyncMock(return_value="DELETE 1")
        self.conn.fetchrow = AsyncMock(return_value=None)

    @contextlib.asynccontextmanager
    async def _acquire(self) -> Any:
        if self.free_after is None:
            await asyncio.Event().wait()
        await asyncio.sleep(self.free_after)
        yield self.conn

    def acquire(self) -> Any:
        return self._acquire()


@pytest.fixture(autouse=True)
def _short_wait(monkeypatch):
    monkeypatch.setattr(responses_store, "GENERAL_ACQUIRE_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(pools, "HISTORY_READ_WAIT_SEC", 0.05)


@pytest.mark.parametrize(
    ("call", "args", "kwargs"),
    [
        ("get", ("resp_1",), {}),
        ("delete", ("resp_1",), {"user_id": "u1"}),
        (
            "save",
            (),
            {"response_id": "resp_1", "user_id": "u1", "response": {}, "messages": []},
        ),
    ],
)
async def test_a_call_serving_a_request_gives_up_on_a_busy_pool(call, args, kwargs):
    store = ResponseStore(_Pool(free_after=None))

    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(DatabaseBusyError):
        await getattr(store, call)(*args, **kwargs)
    assert loop.time() - started < 1


async def test_a_free_connection_serves_the_call():
    pool = _Pool(free_after=0)

    assert await ResponseStore(pool).delete("resp_1", user_id="u1") is True


async def test_the_hard_delete_purge_still_waits_for_a_connection():
    """No client waits on it, and giving up would leave the account's rows behind."""
    pool = _Pool(free_after=0.2)

    assert await ResponseStore(pool).delete_user_responses("u1") == 1


async def test_the_usage_read_is_a_bounded_history_read(monkeypatch):
    calls: list[Any] = []

    @contextlib.asynccontextmanager
    async def refuse(pool: Any, **kwargs: Any) -> Any:
        calls.append(pool)
        raise DatabaseBusyError("no turn")
        yield  # pragma: no cover

    monkeypatch.setattr(postgres_log, "history_read_connection", refuse)
    pool = object()
    store = PostgresLogStore(pool)

    with pytest.raises(DatabaseBusyError):
        await store.get_user_usage_detail("u1", windows=("today",))
    assert calls == [pool]


async def test_the_usage_route_answers_busy_with_a_503_not_zeros():
    op_store = MagicMock()
    op_store.get_active_key_by_account = AsyncMock(return_value={"quota_daily_cost_usd": 5})
    op_store.get_user_cost_today = AsyncMock(return_value=0.0)
    log_store = MagicMock()
    log_store.get_user_usage_detail = AsyncMock(side_effect=DatabaseBusyError("busy"))

    with pytest.raises(DatabaseBusyError):
        await user_routes.get_usage(
            period="today",
            current_user={"user_id": "u1", "role": "free", "max_concurrent_requests": 2},
            op_store=op_store,
            log_store=log_store,
        )
