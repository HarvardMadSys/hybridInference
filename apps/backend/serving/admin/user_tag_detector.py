"""The scheduled run of the user tags' automatic rules (``unattended`` among them).

Every :data:`DETECT_INTERVAL_MINUTES` minutes, each tag with ``auto_enabled``
is given to the accounts its rule matches now: those that sent requests in at
least ``auto_min_active_hours`` of the last ``auto_window_hours`` hours
(:func:`serving.storage.user_tags.detect_members`). A tag it gives stays until
an administrator takes it away (:mod:`serving.user_tags`), so a run only ever
adds members.

One run at a time across every replica: a run holds a transaction-scoped
advisory lock and a replica that cannot take it skips its turn. The writes go
through the run's own connection, not the cached store, so a process serves
the new memberships once its cached entries expire (30 s for an auth context,
60 s for a user row).
"""

from __future__ import annotations

import time
from datetime import UTC
from typing import Any

from serving.storage.user_tags import detect_members
from serving.utils.logging import get_logger

logger = get_logger(__name__)

#: How often the rules run. Ten minutes is a sixth of the smallest window's
#: hour: an account is tagged within ten minutes of matching.
DETECT_INTERVAL_MINUTES = 10

#: The advisory lock one run holds, shared by every replica.
_RUN_LOCK_SQL = (
    "SELECT pg_try_advisory_xact_lock(hashtextextended('hybridinference:user-tag-detector', 0))"
)

#: The tags whose rules run, with their bounds.
_AUTO_TAGS_SQL = (
    "SELECT name, auto_window_hours, auto_min_active_hours FROM user_tags "
    "WHERE auto_enabled ORDER BY name"
)


async def run_rules(pool: Any) -> dict[str, list[str]] | None:
    """Apply every automatic rule once; return the accounts each tag gained.

    None when another replica's run holds the lock. Raises what the database
    raises; :func:`detector_job` is the scheduler's wrapper that never does.
    """
    async with pool.acquire() as conn, conn.transaction():
        if not await conn.fetchval(_RUN_LOCK_SQL):
            return None
        added: dict[str, list[str]] = {}
        for tag in await conn.fetch(_AUTO_TAGS_SQL):
            added[tag["name"]] = await detect_members(
                conn,
                tag["name"],
                window_hours=int(tag["auto_window_hours"]),
                min_active_hours=int(tag["auto_min_active_hours"]),
            )
        return added


async def detector_job(pool: Any) -> None:
    """The scheduled run, for the scheduler: never raises."""
    started_at = time.monotonic()
    outcome = "error"
    added: dict[str, list[str]] | None = None
    try:
        added = await run_rules(pool)
        outcome = "skipped" if added is None else "ok"
    except Exception:
        logger.exception("user_tag_detector failed")
    finally:
        logger.info(
            "user_tag_detector: outcome=%s added=%s duration_ms=%d",
            outcome,
            {tag: len(users) for tag, users in (added or {}).items()},
            int((time.monotonic() - started_at) * 1000),
            extra={
                "event": "user_tag_detector",
                "outcome": outcome,
                "added": {tag: users for tag, users in (added or {}).items() if users},
            },
        )


def register_detector_job(scheduler: Any, pool: Any) -> None:
    """Register the run on the existing AsyncIOScheduler, every few minutes."""
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        detector_job,
        # Offset from the hour's rollups (minutes 5, 10, 15, ...) by two minutes.
        trigger=CronTrigger(minute=f"2-59/{DETECT_INTERVAL_MINUTES}", timezone=UTC),
        args=[pool],
        id="user_tag_detector",
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
    )
    logger.info(
        "user_tag_detector: registered on scheduler (every %d minutes)", DETECT_INTERVAL_MINUTES
    )
