"""A covering index for the admin performance distributions, built in the background.

``/admin/performance-metrics`` computes exact percentiles and histograms over
the successful, non-embedding requests of the last hour, day, week and month.
Through ``idx_api_logs_timestamp`` that reads every one of those rows from the
heap -- on production, about half of a 14 GB table of wide rows, for each of
the four windows' two statements -- and took a minute and a half.

This partial index holds exactly those rows and exactly the columns the
statements read, so they run as index-only scans over a few hundred megabytes.
:data:`ROW_FILTER` is the statements' own row filter, shared with them, so the
planner can always prove the index applies; the query's results are unchanged.

An index-only scan still visits the heap for pages the visibility map does not
mark all-visible -- rows written since ``api_logs`` was last vacuumed -- so the
schema also has autovacuum visit the table after far fewer inserts than its
default (``log_schema``).

The index is not built by the schema initializer, whose ``CREATE INDEX`` holds a
``SHARE`` lock -- blocking every log write -- for the whole build. It is built
here with ``CREATE INDEX CONCURRENTLY``, which lets writes through, in a
background task after startup (:func:`ensure_index`). Until it is valid the
statements work as before, just slowly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    import asyncpg

logger = get_logger(__name__)

INDEX_NAME = "idx_api_logs_perf_metrics"

# The rows the performance distributions describe: successful requests, less
# embeddings, whose latency and token profile would skew chat percentiles. The
# statements filter on this text verbatim, which is what lets the planner match
# them to the partial index.
ROW_FILTER = (
    "status_code BETWEEN 200 AND 399 AND (metadata->>'request_type') IS DISTINCT FROM 'embedding'"
)

_CREATE_SQL = f"""
CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} ON api_logs (timestamp)
INCLUDE (prompt_tokens, completion_tokens, ttft_ms, latency_ms, stream)
WHERE {ROW_FILTER}
"""

# Session-level advisory lock held for the whole build, so one process builds
# while any other started meanwhile skips. Two-part key: a constant class, and
# the schema the index is built in, as each schema has its own.
ADVISORY_LOCK_CLASS = 0x70657266  # ascii "perf" packed
_LOCK_KEY_SQL = "$1::int, hashtext(current_schema())"

# A build reads the whole heap twice and waits out every older transaction --
# the nightly pg_dump's included -- so it must not inherit the pool's 60 s
# command timeout. Generous, not unbounded, so a wedged build is eventually
# abandoned (and its invalid index dropped by the next attempt).
_BUILD_TIMEOUT_SECONDS = 12 * 3600

_STATE_SQL = """
SELECT i.indisvalid
FROM pg_class c
JOIN pg_index i ON i.indexrelid = c.oid
WHERE c.relname = $1 AND c.relnamespace = ANY(
    SELECT oid FROM pg_namespace WHERE nspname = ANY(current_schemas(false))
)
"""


async def ensure_index(pool: asyncpg.Pool) -> bool:
    """Build the index unless it is already valid; True if it is valid on return.

    Skips (False) while another session holds the build lock. An invalid index
    found here, with the lock free, is what a build interrupted part-way leaves
    -- a restart, a cancelled statement -- and is dropped and built again.
    """
    async with pool.acquire() as conn:
        if not await conn.fetchval(
            f"SELECT pg_try_advisory_lock({_LOCK_KEY_SQL})", ADVISORY_LOCK_CLASS
        ):
            logger.info(f"{INDEX_NAME}: another session is building it")
            return False
        try:
            return await _build(conn)
        finally:
            await conn.execute(f"SELECT pg_advisory_unlock({_LOCK_KEY_SQL})", ADVISORY_LOCK_CLASS)


async def _build(conn: asyncpg.Connection) -> bool:
    valid = await conn.fetchval(_STATE_SQL, INDEX_NAME)
    if valid:
        return True
    # Neither statement blocks log writes (SHARE UPDATE EXCLUSIVE), and what
    # they wait for -- older transactions -- is harmless to wait for, so no
    # lock_timeout: a timed-out wait would throw away a finished heap scan.
    await conn.execute("SET lock_timeout = 0")
    try:
        if valid is False:
            logger.warning(f"{INDEX_NAME}: dropping an invalid index left by an earlier build")
            await conn.execute(
                f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}", timeout=_BUILD_TIMEOUT_SECONDS
            )
        logger.info(f"{INDEX_NAME}: building concurrently")
        await conn.execute(_CREATE_SQL, timeout=_BUILD_TIMEOUT_SECONDS)
    finally:
        await conn.execute("SET lock_timeout = DEFAULT")
    logger.info(f"{INDEX_NAME}: built")
    return bool(await conn.fetchval(_STATE_SQL, INDEX_NAME))
