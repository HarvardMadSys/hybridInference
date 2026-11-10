"""Retention for ``auth_sessions``: the rows of sessions that ended go a day later.

Every sign-in writes a session row, and a session ends by expiring or by being
revoked (signing out, a password change, signing out the other devices), but
nothing deleted the rows: the table only grew. An ended session's row is of no
further use. A missing row reads exactly as an ended one everywhere it is read:
session liveness answers "not live" for both, a refresh token whose row is gone
is refused like a revoked one, and a browser's account list shows the account
signed out either way (:func:`serving.auth.browser_sessions.session_live`).

An hourly job (:func:`register_retention_job`) deletes the rows of sessions that
ended more than :data:`ENDED_GRACE_HOURS` ago: expired that long ago, or revoked
and not refreshed since. It deletes in statements of at most :data:`PURGE_BATCH`
rows, each on a pooled connection of its own, and at most
:data:`PURGE_MAX_BATCHES` of them a run, so no statement holds many locks or runs
long and a large backlog is worked off over several runs.
"""

from __future__ import annotations

import time
from datetime import UTC
from typing import Any

from serving.utils.logging import get_logger

logger = get_logger(__name__)

#: How long a session's row is kept after the session ended.
ENDED_GRACE_HOURS = 24

#: Rows deleted per statement.
PURGE_BATCH = 1000

#: Statements per run: a backlog larger than this is finished by later runs.
PURGE_MAX_BATCHES = 50

#: Minute past the hour the purge runs, clear of the rollups (5, 10, 15, 20, 25)
#: and the Responses purge (40).
_PURGE_MINUTE = 45

# Up to $2 sessions that ended over $1 hours ago. A revoked session records no
# time of revocation; it can no longer be refreshed, so its last refresh (or its
# creation) is when it was last in use. ``revoked`` is nullable: only TRUE counts.
_PURGE_SQL = """
DELETE FROM auth_sessions
WHERE id = ANY(ARRAY(
    SELECT id FROM auth_sessions
    WHERE expires_at < now() - make_interval(hours => $1)
       OR (revoked IS TRUE
           AND COALESCE(last_used_at, created_at, expires_at)
               < now() - make_interval(hours => $1))
    LIMIT $2
))
"""


async def purge_ended(
    pool: Any,
    *,
    grace_hours: int = ENDED_GRACE_HOURS,
    batch: int = PURGE_BATCH,
    max_batches: int = PURGE_MAX_BATCHES,
) -> int:
    """Delete the rows of sessions that ended over *grace_hours* ago; return how many.

    In statements of at most *batch* rows, until one finds fewer or
    *max_batches* have run; each takes a pooled connection only for itself.
    """
    purged = 0
    for _ in range(max_batches):
        async with pool.acquire() as conn:
            deleted = _rowcount(await conn.execute(_PURGE_SQL, grace_hours, batch))
        purged += deleted
        if deleted < batch:
            break
    return purged


async def retention_job(pool: Any) -> None:
    """The hourly purge, for the scheduler: never raises."""
    started_at = time.monotonic()
    purged = 0
    outcome = "error"
    try:
        purged = await purge_ended(pool)
        outcome = "ok"
    except Exception:
        logger.exception("auth_sessions_retention failed")
    finally:
        logger.info(
            "auth_sessions_retention: purged=%d duration_ms=%d outcome=%s",
            purged,
            int((time.monotonic() - started_at) * 1000),
            outcome,
        )


def register_retention_job(scheduler: Any, pool: Any) -> None:
    """Register the hourly purge of ended sessions on the existing AsyncIOScheduler."""
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        retention_job,
        trigger=CronTrigger(minute=_PURGE_MINUTE, timezone=UTC),
        args=[pool],
        id="auth_sessions_retention",
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )
    logger.info("auth_sessions_retention: registered on scheduler (cron minute=%d)", _PURGE_MINUTE)


def _rowcount(status: Any) -> int:
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except ValueError:
        return 0
