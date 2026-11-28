"""The scheduled OpenInfra jobs: rollups and retention, and the weekly archives.

Two jobs, each on one replica at a time under a session advisory lock of its
own, so a long archive never holds up the rollups:

- **Maintenance**, one minute past every fifth minute (``1-59/5`` UTC).

  1. Refreshes the rollups (:func:`serving.openinfra.store.refresh_rollups`).
     The first run after a 20-minute bucket ends comes a minute after its last
     sample was taken, when every flush for it has committed; the run after
     that recomputes the bucket once more for a flush that came later still.
     Before that, a sample the previous release wrote with a 6-second bucket
     gets its second's (:func:`serving.openinfra.store.convert_six_second_buckets`).
  2. Deletes samples and request events past the retention
     (:func:`serving.openinfra.archive.prune`): with an archive directory only
     those of weeks already archived; without one, only while
     ``OPENINFRA_DELETE_UNARCHIVED`` says so. A failed refresh skips this, so
     no sample is deleted before its rollups are computed.

- **Archive**, seven minutes past every hour, while there is an archive
  directory: writes each ended week not archived yet
  (:func:`serving.openinfra.archive.archive_ended_weeks`). A failed one leaves
  its week, and every later one, in the database.

Hosts going offline need no job: a read decides that from ``last_seen_at``. A
replica that cannot take a job's lock skips its turn. On an idle fleet each run
is a few statements; a week's archive, once a week, is the long one.
"""

from __future__ import annotations

import time
from datetime import UTC
from typing import Any

from serving.openinfra import archive, config, store
from serving.openinfra.dashboard import now_ms
from serving.utils.logging import get_logger

logger = get_logger(__name__)

#: The advisory lock a maintenance run holds, shared by every replica; session-scoped.
_RUN_LOCK_KEY = "hashtextextended('hybridinference:openinfra-rollups', 0)"
_RUN_LOCK_SQL = f"SELECT pg_try_advisory_lock({_RUN_LOCK_KEY})"
_RUN_UNLOCK_SQL = f"SELECT pg_advisory_unlock({_RUN_LOCK_KEY})"


async def run_maintenance(pool: Any, now: int | None = None) -> bool:
    """Refresh the rollups and apply the retention once; False when another replica holds the lock.

    Raises what the database raises; :func:`maintenance_job` is the
    scheduler's wrapper that never does.
    """
    now = now_ms() if now is None else now
    async with pool.acquire() as conn:
        if not await conn.fetchval(_RUN_LOCK_SQL):
            return False
        try:
            async with conn.transaction():
                converted = await store.convert_six_second_buckets(conn)
                await store.refresh_rollups(conn, now)
            if converted:
                logger.info(
                    "openinfra: %d samples with 6-second buckets moved to their second, or "
                    "dropped where it already had one",
                    converted,
                )
            if config.archive_dir() is not None:
                deleted = await archive.prune(
                    conn, now, config.retention_days(), archived_only=True
                )
            elif config.delete_unarchived():
                deleted = await archive.prune(
                    conn, now, config.retention_days(), archived_only=False
                )
            else:
                deleted = {}
            if any(deleted.values()):
                logger.info(
                    "openinfra retention: deleted %s",
                    ", ".join(f"{count} from {table}" for table, count in deleted.items()),
                )
            return True
        finally:
            # The connection goes back to the pool: the lock must not go with it.
            await conn.execute(_RUN_UNLOCK_SQL)


async def run_archive(pool: Any, now: int | None = None) -> list[int] | None:
    """Archive the ended weeks once; the weeks written, or None when another replica holds the lock.

    Nothing to do without an archive directory. Raises what the database or
    the filesystem raises; :func:`archive_job` is the scheduler's wrapper.
    """
    directory = config.archive_dir()
    if directory is None:
        return []
    now = now_ms() if now is None else now
    async with pool.acquire() as conn:
        if not await conn.fetchval(archive.TRY_LOCK_SQL):
            return None
        try:
            return await archive.archive_ended_weeks(conn, directory, now)
        finally:
            await conn.execute(archive.UNLOCK_SQL)


async def maintenance_job(pool: Any) -> None:
    """The scheduled maintenance run, for the scheduler: never raises."""
    started_at = time.monotonic()
    outcome = "error"
    try:
        outcome = "ok" if await run_maintenance(pool) else "skipped"
    except Exception:
        logger.exception("openinfra maintenance failed")
    finally:
        logger.info(
            "openinfra_maintenance: outcome=%s duration_ms=%d",
            outcome,
            int((time.monotonic() - started_at) * 1000),
            extra={"event": "openinfra_maintenance", "outcome": outcome},
        )


async def archive_job(pool: Any) -> None:
    """The scheduled archive run, for the scheduler: never raises."""
    started_at = time.monotonic()
    outcome = "error"
    try:
        written = await run_archive(pool)
        outcome = "skipped" if written is None else "ok"
    except Exception:
        logger.exception(
            "openinfra archive to %s failed; its weeks stay in the database", config.archive_dir()
        )
    finally:
        logger.info(
            "openinfra_archive: outcome=%s duration_ms=%d",
            outcome,
            int((time.monotonic() - started_at) * 1000),
            extra={"event": "openinfra_archive", "outcome": outcome},
        )


def register_maintenance_job(scheduler: Any, pool: Any) -> None:
    """Register both jobs on the existing AsyncIOScheduler."""
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        maintenance_job,
        trigger=CronTrigger(minute="1-59/5", timezone=UTC),
        args=[pool],
        id="openinfra_maintenance",
        replace_existing=True,
        misfire_grace_time=240,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        archive_job,
        trigger=CronTrigger(minute="7", timezone=UTC),
        args=[pool],
        id="openinfra_archive",
        replace_existing=True,
        misfire_grace_time=1800,
        coalesce=True,
        max_instances=1,
    )
    logger.info("openinfra_maintenance: registered on scheduler (every 5 minutes; archives hourly)")
