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
  kept open so the steady state never waits for a new connection.
- at most 14 connections in all (18 with the alert-history pool's 4), well
  under PostgreSQL's default ``max_connections`` of 100.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

#: The general pool's size: unchanged from the single pool it was.
GENERAL_POOL_MIN_SIZE = 2
GENERAL_POOL_MAX_SIZE = 10

#: The request pool's size; see the module docstring for the arithmetic.
REQUEST_POOL_MIN_SIZE = 2
REQUEST_POOL_MAX_SIZE = 4

#: Statement timeout on both pools, in seconds: unchanged.
COMMAND_TIMEOUT_SEC = 60

#: How long a request-path lookup waits for a connection. About a thousand
#: times what one lookup takes, so it trips only when the database or the
#: request pool is stalled, not under a burst; the same bound the alert-history
#: pool uses.
REQUEST_ACQUIRE_TIMEOUT_SEC = 3.0

#: The ``Retry-After`` a refused request is given, in seconds.
DATABASE_BUSY_RETRY_AFTER_SEC = 1

#: The 503's message. Static, so it carries nothing about the database.
DATABASE_BUSY_DETAIL = (
    "The service is busy and could not check this request in time. Retry shortly."
)


class DatabaseBusyError(RuntimeError):
    """No request-path connection came free within :data:`REQUEST_ACQUIRE_TIMEOUT_SEC`.

    Answered with a 503 and ``Retry-After`` by the app's exception handler.
    """


@contextlib.asynccontextmanager
async def request_connection(pool: Any, *, timeout: float | None = None) -> AsyncIterator[Any]:
    """A connection from *pool* for a request-path lookup, waited for at most *timeout*.

    *timeout* defaults to :data:`REQUEST_ACQUIRE_TIMEOUT_SEC`. Only the wait for
    the connection is bounded: a statement that then runs long is the pool's
    ``command_timeout``'s to stop, and its error is not turned into this one.

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
