"""The scheduled OpenInfra rollup refresh (:func:`serving.openinfra.store.refresh_rollups`).

It runs one minute past every fifth minute (``1-59/5`` UTC), so the first run
after a 20-minute bucket ends comes a minute after its last sample was taken,
when every flush for it has committed; the run after that recomputes the
bucket once more for a flush that came later still. Hosts going offline need
no job: a read decides that from ``last_seen_at``.

One run at a time across every replica: a run holds a session advisory lock on
its connection, and a replica that cannot take it skips its turn. The refresh
is a few statements over at most the last two buckets of each grain, so it is
cheap on an idle fleet and a no-op on a deployment that collects nothing.
"""

from __future__ import annotations

import time
from datetime import UTC
from typing import Any

from serving.openinfra import store
from serving.openinfra.dashboard import now_ms
from serving.utils.logging import get_logger

logger = get_logger(__name__)

#: The advisory lock one run holds, shared by every replica; session-scoped.
_RUN_LOCK_KEY = "hashtextextended('hybridinference:openinfra-rollups', 0)"
_RUN_LOCK_SQL = f"SELECT pg_try_advisory_lock({_RUN_LOCK_KEY})"
_RUN_UNLOCK_SQL = f"SELECT pg_advisory_unlock({_RUN_LOCK_KEY})"


async def run_refresh(pool: Any, now: int | None = None) -> bool:
    """Refresh the rollups once; False when another replica's run holds the lock.

    Raises what the database raises; :func:`refresh_job` is the scheduler's
    wrapper that never does.
    """
    async with pool.acquire() as conn:
        if not await conn.fetchval(_RUN_LOCK_SQL):
            return False
        try:
            async with conn.transaction():
                await store.refresh_rollups(conn, now_ms() if now is None else now)
            return True
        finally:
            # The connection goes back to the pool: the lock must not go with it.
            await conn.execute(_RUN_UNLOCK_SQL)


async def refresh_job(pool: Any) -> None:
    """The scheduled run, for the scheduler: never raises."""
    started_at = time.monotonic()
    outcome = "error"
    try:
        outcome = "ok" if await run_refresh(pool) else "skipped"
    except Exception:
        logger.exception("openinfra rollup refresh failed")
    finally:
        logger.info(
            "openinfra_rollups: outcome=%s duration_ms=%d",
            outcome,
            int((time.monotonic() - started_at) * 1000),
            extra={"event": "openinfra_rollups", "outcome": outcome},
        )


def register_refresh_job(scheduler: Any, pool: Any) -> None:
    """Register the refresh on the existing AsyncIOScheduler."""
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        refresh_job,
        trigger=CronTrigger(minute="1-59/5", timezone=UTC),
        args=[pool],
        id="openinfra_rollups",
        replace_existing=True,
        misfire_grace_time=240,
        coalesce=True,
        max_instances=1,
    )
    logger.info("openinfra_rollups: registered on scheduler (every 5 minutes)")
