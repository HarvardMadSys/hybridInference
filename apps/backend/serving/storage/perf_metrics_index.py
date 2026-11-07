"""Covering indexes for the admin views over ``api_logs``, built in the background.

**The performance distributions** (:data:`INDEX_NAME`).
``/admin/performance-metrics`` computes exact percentiles and histograms over
the successful, non-embedding requests of the last hour, day, week and month.
Through ``idx_api_logs_timestamp`` that reads every one of those rows from the
heap -- on production, about half of a 14 GB table of wide rows, for each of
the four windows' two statements -- and took a minute and a half.

This partial index holds exactly those rows and exactly the columns the
statements read, so they run as index-only scans over a few hundred megabytes.
:data:`ROW_FILTER` is the statements' own row filter, shared with them, so the
planner can always prove the index applies; the query's results are unchanged.

**The request lists' source** (:data:`SOURCE_INDEX_NAME`). The admin Recent
Requests tab lists the requests callers made directly (``batch_job_id IS
NULL``) and Batches → Requests the batch worker's (``IS NOT NULL``, or one
batch's), each with a total counted over the lookback, a week by default
(``_common.REQUEST_SOURCE_FILTERS``). Unfiltered, that total is an index-only
scan of ``idx_api_logs_timestamp``; with the source filter it read every row of
the week from the heap to see its ``batch_job_id``. Over 2.4 million rows,
cached, that took the list from about 50 ms to 900 ms, and it grows with the
table toward the count's time budget. ``(timestamp, batch_job_id)`` puts the
column in the index: each source's total, and one batch's, is an index-only
scan again, and a page of either reads from the heap only the rows it lists.

An index-only scan still visits the heap for pages the visibility map does not
mark all-visible -- rows written since ``api_logs`` was last vacuumed -- so the
schema also has autovacuum visit the table after far fewer inserts than its
default (a storage parameter the schema migrations set).

The indexes are not built by the schema migrations, whose ``CREATE INDEX`` would
hold a ``SHARE`` lock -- blocking every log write -- for the whole build. They
are built here with ``CREATE INDEX CONCURRENTLY``, which lets writes through, in
a background task after startup (:func:`ensure_index`). Until one is valid the
statements it serves work as before, just slowly.
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

SOURCE_INDEX_NAME = "idx_api_logs_source"

# Not partial: one index serves both sources' predicates, and one batch's
# (``batch_job_id = $n`` within the window) as an index condition rather than a
# heap read. Leading on timestamp, descending like idx_api_logs_timestamp, so the
# lists' ``ORDER BY timestamp DESC LIMIT`` reads it in order.
_SOURCE_CREATE_SQL = f"""
CREATE INDEX CONCURRENTLY IF NOT EXISTS {SOURCE_INDEX_NAME}
ON api_logs (timestamp DESC, batch_job_id)
"""

#: Every index built here, as ``(name, statement)``, in the order they are built.
INDEXES: tuple[tuple[str, str], ...] = (
    (INDEX_NAME, _CREATE_SQL),
    (SOURCE_INDEX_NAME, _SOURCE_CREATE_SQL),
)

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
    """Build each of :data:`INDEXES` that is not already valid; True if all are on return.

    Skips (False) while another session holds the build lock. An invalid index
    found here, with the lock free, is what a build interrupted part-way leaves
    -- a restart, a cancelled statement -- and is dropped and built again. A
    build that fails is logged and does not keep the next index from being
    built: each serves its own views.
    """
    async with pool.acquire() as conn:
        if not await conn.fetchval(
            f"SELECT pg_try_advisory_lock({_LOCK_KEY_SQL})", ADVISORY_LOCK_CLASS
        ):
            logger.info("api_logs background indexes: another session is building them")
            return False
        try:
            all_valid = True
            for name, create_sql in INDEXES:
                try:
                    valid = await _build(conn, name, create_sql)
                except Exception:
                    logger.exception(f"{name}: build failed")
                    valid = False
                all_valid = all_valid and valid
            return all_valid
        finally:
            await conn.execute(f"SELECT pg_advisory_unlock({_LOCK_KEY_SQL})", ADVISORY_LOCK_CLASS)


async def _build(conn: asyncpg.Connection, name: str, create_sql: str) -> bool:
    valid = await conn.fetchval(_STATE_SQL, name)
    if valid:
        return True
    # Neither statement blocks log writes (SHARE UPDATE EXCLUSIVE), and what
    # they wait for -- older transactions -- is harmless to wait for, so no
    # lock_timeout: a timed-out wait would throw away a finished heap scan.
    await conn.execute("SET lock_timeout = 0")
    try:
        if valid is False:
            logger.warning(f"{name}: dropping an invalid index left by an earlier build")
            await conn.execute(
                f"DROP INDEX CONCURRENTLY IF EXISTS {name}", timeout=_BUILD_TIMEOUT_SECONDS
            )
        logger.info(f"{name}: building concurrently")
        await conn.execute(create_sql, timeout=_BUILD_TIMEOUT_SECONDS)
    finally:
        await conn.execute("SET lock_timeout = DEFAULT")
    logger.info(f"{name}: built")
    return bool(await conn.fetchval(_STATE_SQL, name))
