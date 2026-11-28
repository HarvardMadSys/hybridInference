"""The scheduled OpenInfra maintenance: rollups, weekly archives and retention.

Each run, one minute past every fifth minute (``1-59/5`` UTC):

1. refreshes the rollups (:func:`serving.openinfra.store.refresh_rollups`). The
   first run after a 20-minute bucket ends comes a minute after its last
   sample was taken, when every flush for it has committed; the run after that
   recomputes the bucket once more for a flush that came later still. Before
   that, a sample the previous release wrote with a 6-second bucket gets its
   second's (:func:`serving.openinfra.store.convert_six_second_buckets`);
2. archives each ended week not archived yet, while there is an archive
   directory (:func:`serving.openinfra.archive.archive_ended_weeks`);
3. deletes samples and request events past the retention
   (:func:`serving.openinfra.archive.prune`), only from archived weeks while
   there is an archive directory.

A failed refresh skips the retention as well, so no sample is deleted before
its rollups are computed. A failed archive is logged and leaves its week, and
every later one, in the database. Hosts going offline need no job: a read
decides that from ``last_seen_at``.

One run at a time across every replica: a run holds a session advisory lock on
its connection, and a replica that cannot take it skips its turn. A run is a
few statements over at most the last two buckets of each grain and the few
minutes of samples that crossed the retention since the last, so it is cheap
on an idle fleet and a no-op on a deployment that collects nothing; a week's
archive, once a week, is the long one.
"""

from __future__ import annotations

import time
from datetime import UTC
from typing import Any

from serving.openinfra import archive, config, store
from serving.openinfra.dashboard import now_ms
from serving.utils.logging import get_logger

logger = get_logger(__name__)

#: The advisory lock one run holds, shared by every replica; session-scoped.
_RUN_LOCK_KEY = "hashtextextended('hybridinference:openinfra-rollups', 0)"
_RUN_LOCK_SQL = f"SELECT pg_try_advisory_lock({_RUN_LOCK_KEY})"
_RUN_UNLOCK_SQL = f"SELECT pg_advisory_unlock({_RUN_LOCK_KEY})"


async def run_maintenance(pool: Any, now: int | None = None) -> bool:
    """Run the maintenance once; False when another replica's run holds the lock.

    Raises what the database raises while refreshing the rollups or deleting;
    a failed archive is logged instead. :func:`maintenance_job` is the
    scheduler's wrapper that never raises.
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
                    "openinfra: gave %d samples with 6-second buckets their second", converted
                )
            directory = config.archive_dir()
            if directory is not None:
                try:
                    await archive.archive_ended_weeks(conn, directory, now)
                except Exception:
                    logger.exception(
                        "openinfra archive to %s failed; its weeks stay in the database", directory
                    )
            deleted = await archive.prune(
                conn, now, config.retention_days(), archived_only=directory is not None
            )
            if any(deleted.values()):
                logger.info(
                    "openinfra retention: deleted %s",
                    ", ".join(f"{count} from {table}" for table, count in deleted.items()),
                )
            return True
        finally:
            # The connection goes back to the pool: the lock must not go with it.
            await conn.execute(_RUN_UNLOCK_SQL)


async def maintenance_job(pool: Any) -> None:
    """The scheduled run, for the scheduler: never raises."""
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


def register_maintenance_job(scheduler: Any, pool: Any) -> None:
    """Register the maintenance on the existing AsyncIOScheduler."""
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
    logger.info("openinfra_maintenance: registered on scheduler (every 5 minutes)")
