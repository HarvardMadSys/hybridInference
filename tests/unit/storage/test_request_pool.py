"""The request path has connections of its own, and never waits long for one.

One pool of ten served everything, with no bound on the wait for a connection:
slow reports, exports and rollups holding all ten made every request's auth
lookup queue behind them. The lookups a request makes before it is served now
use a small pool nothing else touches, and give up with a clear 503 after a
short wait instead of hanging.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from serving.storage import database, pools
from serving.storage.database import DatabaseLogger
from serving.storage.pools import DatabaseBusyError, request_connection
from serving.storage.postgres_operational import REQUEST_PATH_READS, PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = pytest.mark.asyncio


class _Pool:
    """A pool of *size* fake connections that records who took one."""

    def __init__(self, name: str, size: int = 2) -> None:
        self.name = name
        self._free = asyncio.Semaphore(size)
        self.acquired = 0
        self.held = 0
        self.conn = MagicMock(name=f"{name}-conn")
        self.conn.fetchrow = AsyncMock(return_value=None)
        self.conn.fetchval = AsyncMock(return_value=0)
        self.conn.fetch = AsyncMock(return_value=[])
        self.conn.execute = AsyncMock(return_value="UPDATE 1")

    @contextlib.asynccontextmanager
    async def _hold(self):
        async with self._free:
            self.acquired += 1
            self.held += 1
            try:
                yield self.conn
            finally:
                self.held -= 1

    def acquire(self) -> Any:
        return self._hold()


def _call_args(name: str) -> tuple[list[Any], dict[str, Any]]:
    return {
        "get_user_cost_period": (["u1", "month"], {}),
        "session_is_live": (["u1", "sid"], {}),
        "get_key_spend": ([1], {}),
    }.get(name, (["x"], {}))


# ---------------------------------------------------------------------------
# request_connection
# ---------------------------------------------------------------------------


async def test_a_free_connection_is_handed_out_and_given_back():
    pool = _Pool("request", size=1)

    async with request_connection(pool) as conn:
        assert conn is pool.conn
        assert pool.held == 1
    assert pool.held == 0


async def test_an_exhausted_pool_fails_fast_with_a_busy_error():
    pool = _Pool("request", size=1)
    async with pool.acquire():
        started = time.monotonic()
        with pytest.raises(DatabaseBusyError):
            async with request_connection(pool, timeout=0.05):
                pass
        assert time.monotonic() - started < 1.0
    # The abandoned wait took nothing with it.
    async with request_connection(pool, timeout=0.05):
        assert pool.held == 1


async def test_the_default_wait_is_the_module_bound(monkeypatch):
    monkeypatch.setattr(pools, "REQUEST_ACQUIRE_TIMEOUT_SEC", 0.05)
    pool = _Pool("request", size=1)
    async with pool.acquire():
        with pytest.raises(DatabaseBusyError):
            async with request_connection(pool):
                pass


async def test_a_slow_statement_is_not_reported_as_a_busy_pool():
    """Only the wait for a connection is bounded here; a statement timeout stays itself."""
    pool = _Pool("request", size=1)

    with pytest.raises(TimeoutError) as raised:
        async with request_connection(pool, timeout=0.05):
            raise TimeoutError("statement timeout")
    assert not isinstance(raised.value, DatabaseBusyError)
    assert pool.held == 0


def test_the_wait_is_seconds_not_minutes():
    assert 1 <= pools.REQUEST_ACQUIRE_TIMEOUT_SEC <= 5
    # A request-pool statement gone slow gives its connection back in seconds:
    # four of them held for the general pool's minute would refuse every request.
    assert 1 <= pools.REQUEST_COMMAND_TIMEOUT_SEC <= 10
    assert pools.REQUEST_COMMAND_TIMEOUT_SEC < pools.COMMAND_TIMEOUT_SEC
    assert pools.REQUEST_POOL_MAX_SIZE < pools.GENERAL_POOL_MAX_SIZE
    # Both pools together stay far below PostgreSQL's default of 100.
    assert pools.REQUEST_POOL_MAX_SIZE + pools.GENERAL_POOL_MAX_SIZE <= 20


# ---------------------------------------------------------------------------
# Which pool each lookup uses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", REQUEST_PATH_READS)
async def test_request_path_lookups_use_the_request_pool(name):
    general, request = _Pool("general"), _Pool("request")
    store = PostgresOperationalStore(general, request_pool=request)
    args, kwargs = _call_args(name)

    with contextlib.suppress(Exception):  # the fake rows are not real rows
        await getattr(store, name)(*args, **kwargs)

    assert (request.acquired, general.acquired) == (1, 0)


@pytest.mark.parametrize("name", REQUEST_PATH_READS)
async def test_request_path_lookups_do_not_wait_behind_the_general_pool(name):
    """Every general connection held by slow work: a request is still served."""
    general, request = _Pool("general", size=1), _Pool("request")
    store = PostgresOperationalStore(general, request_pool=request)
    args, kwargs = _call_args(name)

    async with general.acquire():
        with contextlib.suppress(Exception):
            await asyncio.wait_for(getattr(store, name)(*args, **kwargs), timeout=1.0)

    assert request.acquired == 1


async def test_background_and_fire_and_forget_work_stays_on_the_general_pool():
    general, request = _Pool("general"), _Pool("request")
    store = PostgresOperationalStore(general, request_pool=request)

    await store.update_key_last_used(1)
    await store.health_check()
    await store.get_key_owner_for_audit("h")

    assert (request.acquired, general.acquired) == (0, 3)


async def test_without_a_request_pool_the_lookups_use_the_one_pool_with_the_bound(monkeypatch):
    monkeypatch.setattr(pools, "REQUEST_ACQUIRE_TIMEOUT_SEC", 0.05)
    only = _Pool("only", size=1)
    store = PostgresOperationalStore(only)

    await store.session_is_live("u1", "sid")
    assert only.acquired == 1
    async with only.acquire():
        with pytest.raises(DatabaseBusyError):
            await store.session_is_live("u1", "sid")


async def test_an_exhausted_request_pool_refuses_quickly(monkeypatch):
    monkeypatch.setattr(pools, "REQUEST_ACQUIRE_TIMEOUT_SEC", 0.05)
    general, request = _Pool("general"), _Pool("request", size=1)
    store = PostgresOperationalStore(general, request_pool=request)

    async with request.acquire():
        with pytest.raises(DatabaseBusyError):
            await store.get_auth_context_with_spend("hash")


# ---------------------------------------------------------------------------
# DatabaseLogger opens and closes both
# ---------------------------------------------------------------------------


@pytest.fixture
def created(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_create_pool(**kwargs):
        pool = MagicMock(name=f"pool-{len(calls)}")
        pool.close = AsyncMock()
        calls.append({**kwargs, "pool": pool})
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    return calls


async def test_the_logger_opens_a_general_and_a_request_pool(created):
    logger = DatabaseLogger({"host": "db"})

    await logger.initialize()

    sizes = [(c["min_size"], c["max_size"], c["command_timeout"]) for c in created]
    assert sizes == [
        (pools.GENERAL_POOL_MIN_SIZE, pools.GENERAL_POOL_MAX_SIZE, pools.COMMAND_TIMEOUT_SEC),
        (
            pools.REQUEST_POOL_MIN_SIZE,
            pools.REQUEST_POOL_MAX_SIZE,
            pools.REQUEST_COMMAND_TIMEOUT_SEC,
        ),
    ]
    assert (logger.pool, logger.request_pool) == (created[0]["pool"], created[1]["pool"])

    await logger.cleanup()
    created[0]["pool"].close.assert_awaited_once()
    created[1]["pool"].close.assert_awaited_once()
    assert (logger.pool, logger.request_pool) == (None, None)


async def test_a_failed_start_leaves_no_pool_open(created, monkeypatch):
    """Bootstrap retries a failed start with a fresh call; the first one's pools must close."""

    async def refuse(conn, *, secret):
        raise ConnectionError("database went away")

    monkeypatch.setattr(database, "validate_or_init_fingerprint", refuse)
    for c in created:
        c["pool"].acquire = MagicMock()
    logger = DatabaseLogger({"host": "db"}, fence_secret=postgres.FENCE_SECRET)

    with pytest.raises(ConnectionError):
        await logger.initialize()

    assert [c["pool"].close.await_count for c in created] == [1, 1]
    assert (logger.pool, logger.request_pool) == (None, None)


async def test_bootstrap_hands_the_request_pool_to_the_operational_store():
    import inspect

    from serving.servers import bootstrap

    source = inspect.getsource(bootstrap)
    assert 'request_pool=getattr(db_logger, "request_pool", None)' in source
