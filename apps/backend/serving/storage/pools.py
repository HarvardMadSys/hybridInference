"""The gateway's two PostgreSQL connection pools, and the request path's bounded wait.

One asyncpg pool used to serve everything: the auth and spend lookup every API
request makes, the session check every console request makes, and the log
writers, batch work, hourly rollups, admin analytics and exports. Nothing
bounded a wait for one of its ten connections, so when slow work held them all,
a request's auth lookup queued behind it for as long as that work took.

The cheap lookups a request makes before it is served now have a pool of their
own, the *request pool* (:data:`REQUEST_POOL_MAX_SIZE` connections), that
nothing else uses; everything else keeps the *general pool* as it was. A
request-path lookup waits at most :data:`REQUEST_ACQUIRE_TIMEOUT_SEC` for a
connection and then fails with :class:`DatabaseBusyError`, which the app
answers with a 503 and ``Retry-After`` instead of letting the request hang.

Sizes, per gateway process:

- general pool: 2 to 10 connections, unchanged. Log writers, batch, rollups,
  admin views, exports and everything not listed below keep the capacity they
  had.
- request pool: 2 to 4. Its queries are primary-key and index lookups that
  take about a millisecond, one or two per request, so four connections serve
  thousands a second -- far more than a gateway process handles -- and two are
  kept open so the steady state never waits for a new connection. A statement
  on it is stopped after :data:`REQUEST_COMMAND_TIMEOUT_SEC`: with four
  connections, one lookup gone slow must not hold a quarter of the pool for the
  general pool's minute.
- at most 14 connections in all (18 with the alert-history pool's 4), well
  under PostgreSQL's default ``max_connections`` of 100.

A user's reads of their own request history on the general pool
(``/user/recent-requests``, ``/user/usage``) hold at most
:data:`HISTORY_READS_MAX` general-pool connections at once in a process,
however many users ask (:func:`history_read_connection`), so the log writers,
batch work and rollups always keep the rest of the pool. One waits at most
:data:`HISTORY_READ_WAIT_SEC` for its turn and its connection, and then fails
with :class:`DatabaseBusyError` like a request-path lookup. The Responses store's
loads and saves, made while a request is served, wait at most
:data:`GENERAL_ACQUIRE_TIMEOUT_SEC` for a general-pool connection the same way
(:func:`request_connection` with that timeout). Neither opens a connection of
its own: the budget above is unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import weakref
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

#: The general pool's size: unchanged from the single pool it was.
GENERAL_POOL_MIN_SIZE = 2
GENERAL_POOL_MAX_SIZE = 10

#: The request pool's size; see the module docstring for the arithmetic.
REQUEST_POOL_MIN_SIZE = 2
REQUEST_POOL_MAX_SIZE = 4

#: Statement timeout on the general pool, in seconds: unchanged.
COMMAND_TIMEOUT_SEC = 60

#: Statement timeout on the request pool, in seconds. Thousands of times what
#: one of its lookups takes, so it trips only on a stalled lookup, which then
#: gives its connection back rather than keeping it for a minute.
REQUEST_COMMAND_TIMEOUT_SEC = 5

#: How long a request-path lookup waits for a connection. About a thousand
#: times what one lookup takes, so it trips only when the database or the
#: request pool is stalled, not under a burst; the same bound the alert-history
#: pool uses.
REQUEST_ACQUIRE_TIMEOUT_SEC = 3.0

#: How long a store call made while serving a request waits for a general-pool
#: connection: the Responses store's loads and saves. Its own statements are
#: single-row; a wait this long means slow work holds the pool, and the request
#: is answered (a 503 with ``Retry-After``, or a response left unstored) rather
#: than left hanging behind it.
GENERAL_ACQUIRE_TIMEOUT_SEC = 5.0

#: How many general-pool connections users' reads of their own request history
#: may hold at once, per process: under half the pool, so the log writers keep
#: most of it whatever users ask for.
HISTORY_READS_MAX = 4

#: How long one such read waits for its turn and its connection together.
HISTORY_READ_WAIT_SEC = 5.0

#: The ``Retry-After`` a refused request is given, in seconds.
DATABASE_BUSY_RETRY_AFTER_SEC = 1

#: The 503's message. Static, so it carries nothing about the database.
DATABASE_BUSY_DETAIL = (
    "The service is busy and could not check this request in time. Retry shortly."
)


class DatabaseBusyError(RuntimeError):
    """No connection came free in time for the request being served.

    For a request-path lookup within :data:`REQUEST_ACQUIRE_TIMEOUT_SEC`, for a
    history read within :data:`HISTORY_READ_WAIT_SEC`, for another call made
    while serving a request within :data:`GENERAL_ACQUIRE_TIMEOUT_SEC`. Answered
    with a 503 and ``Retry-After`` by the app's exception handler.
    """


@contextlib.asynccontextmanager
async def request_connection(pool: Any, *, timeout: float | None = None) -> AsyncIterator[Any]:
    """A connection from *pool* for a request-path lookup, waited for at most *timeout*.

    *timeout* defaults to :data:`REQUEST_ACQUIRE_TIMEOUT_SEC`. Only the wait for
    the connection is bounded here: a statement that then runs long is the
    pool's ``command_timeout``'s to stop (:data:`REQUEST_COMMAND_TIMEOUT_SEC` on
    the request pool), and its error is not turned into this one.

    Raises:
        DatabaseBusyError: No connection came free in time.
    """
    if timeout is None:
        timeout = REQUEST_ACQUIRE_TIMEOUT_SEC
    async with contextlib.AsyncExitStack() as stack:
        try:
            async with asyncio.timeout(timeout):
                conn = await stack.enter_async_context(pool.acquire())
        except TimeoutError as exc:
            raise DatabaseBusyError(
                f"no request-path database connection within {timeout:g}s"
            ) from exc
        yield conn


class _LoopSemaphore:
    """An :class:`asyncio.Semaphore` per running event loop (tests run many; production one)."""

    def __init__(self, value: int) -> None:
        self._value = value
        self._by_loop: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
            weakref.WeakKeyDictionary()
        )

    def get(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        semaphore = self._by_loop.get(loop)
        if semaphore is None:
            semaphore = self._by_loop[loop] = asyncio.Semaphore(self._value)
        return semaphore


_HISTORY_READS = _LoopSemaphore(HISTORY_READS_MAX)


@contextlib.asynccontextmanager
async def history_read_connection(pool: Any, *, timeout: float | None = None) -> AsyncIterator[Any]:
    """A connection from *pool* for a user's read of their own request history.

    At most :data:`HISTORY_READS_MAX` are held at once in this process, whoever
    asks; the rest wait their turn. The turn and the connection together are
    waited for at most *timeout* (default :data:`HISTORY_READ_WAIT_SEC`).

    Raises:
        DatabaseBusyError: No turn and connection came free in time.
    """
    if timeout is None:
        timeout = HISTORY_READ_WAIT_SEC
    semaphore = _HISTORY_READS.get()
    async with contextlib.AsyncExitStack() as stack:
        try:
            async with asyncio.timeout(timeout):
                await semaphore.acquire()
                stack.callback(semaphore.release)
                conn = await stack.enter_async_context(pool.acquire())
        except TimeoutError as exc:
            raise DatabaseBusyError(
                f"no database connection for a history read within {timeout:g}s"
            ) from exc
        yield conn


class SingleConnectionPool:
    """One connection behind a pool's ``acquire()``, for stores that share a transaction.

    Every ``acquire()`` yields the same connection and releases nothing, so a
    store built on it runs each call on that connection, inside whatever
    transaction its owner holds open; a method's own ``conn.transaction()``
    becomes a savepoint there. The owner opens, commits and closes the
    connection. Calls must not overlap: a connection runs one query at a time.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @contextlib.asynccontextmanager
    async def acquire(self, *, timeout: float | None = None) -> AsyncIterator[Any]:
        """Yield the one connection; *timeout* is accepted for the pool's signature."""
        yield self._connection
