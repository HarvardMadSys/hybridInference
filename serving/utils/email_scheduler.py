"""APScheduler-backed broadcast email scheduler."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from serving.utils.email import send_email
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    import asyncpg

logger = get_logger(__name__)

_scheduler: AsyncIOScheduler | None = None
_db_pool: asyncpg.Pool | None = None
_background_tasks: set[asyncio.Task] = set()

BATCH_SIZE = 50
BATCH_DELAY_SECONDS = 1.0


def _spawn_background(coro) -> None:
    """Create an asyncio task and hold a reference until done."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def start_scheduler(db_pool: asyncpg.Pool) -> None:
    """Start APScheduler and store the DB pool for use by scheduled jobs."""
    global _scheduler, _db_pool
    _db_pool = db_pool
    _scheduler = AsyncIOScheduler()
    _scheduler.start()
    logger.info("Broadcast email scheduler started")


def stop_scheduler() -> None:
    """Shut down APScheduler gracefully."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Broadcast email scheduler stopped")
    _scheduler = None


async def rehydrate_scheduled_broadcasts() -> None:
    """Re-register scheduled broadcasts into APScheduler on startup (restart recovery)."""
    if not _db_pool:
        return
    now = datetime.now(timezone.utc)
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, scheduled_at FROM email_broadcasts WHERE status = 'scheduled'"
        )
    for row in rows:
        broadcast_id = row["id"]
        run_at = row["scheduled_at"]
        if run_at is None or run_at <= now:
            logger.info(f"Rehydrating missed broadcast {broadcast_id} — firing immediately")
            _spawn_background(execute_broadcast(broadcast_id))
        else:
            _add_scheduler_job(broadcast_id, run_at)
            logger.info(f"Rehydrated scheduled broadcast {broadcast_id} for {run_at}")


def schedule_broadcast(broadcast_id: str, run_at: datetime | None) -> None:
    """Schedule a broadcast. If run_at is None, fire immediately as a background task."""
    if run_at is None:
        _spawn_background(execute_broadcast(broadcast_id))
    else:
        _add_scheduler_job(broadcast_id, run_at)


def cancel_broadcast_job(broadcast_id: str) -> None:
    """Remove a scheduled APScheduler job. No-op if not found."""
    if _scheduler:
        with contextlib.suppress(Exception):
            _scheduler.remove_job(broadcast_id)


def _add_scheduler_job(broadcast_id: str, run_at: datetime) -> None:
    if not _scheduler:
        raise RuntimeError("Scheduler not started")
    _scheduler.add_job(
        _run_broadcast_sync,
        trigger=DateTrigger(run_date=run_at),
        id=broadcast_id,
        args=[broadcast_id],
        replace_existing=True,
    )


def _run_broadcast_sync(broadcast_id: str) -> None:
    """Sync wrapper called by APScheduler — creates asyncio task."""
    _spawn_background(execute_broadcast(broadcast_id))


async def execute_broadcast(broadcast_id: str) -> None:
    """Send emails for a broadcast and update per-recipient + broadcast status in DB."""
    if not _db_pool:
        logger.error(f"Cannot execute broadcast {broadcast_id}: no DB pool")
        return

    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM email_broadcasts WHERE id = $1", broadcast_id)
        if not row:
            logger.error(f"Broadcast {broadcast_id} not found")
            return

        await conn.execute(
            "UPDATE email_broadcasts SET status = 'sending' WHERE id = $1", broadcast_id
        )

        roles = list(row["target_roles"]) or []
        statuses = list(row["target_statuses"]) or []

        recipients = await conn.fetch(
            """
            SELECT id, email FROM users
            WHERE role = ANY($1::text[])
              AND status = ANY($2::text[])
            """,
            roles,
            statuses,
        )

        if recipients:
            await conn.executemany(
                """
                INSERT INTO email_broadcast_recipients (broadcast_id, user_id, email, status)
                VALUES ($1, $2, $3, 'pending')
                ON CONFLICT DO NOTHING
                """,
                [(broadcast_id, r["id"], r["email"]) for r in recipients],
            )

        await conn.execute(
            "UPDATE email_broadcasts SET recipient_count = $1 WHERE id = $2",
            len(recipients),
            broadcast_id,
        )

    subject = row["subject"]
    body_html = row["body_html"]
    body_text = row["body_text"]

    sent_count = 0
    failed_count = 0

    for i in range(0, len(recipients), BATCH_SIZE):
        batch = recipients[i : i + BATCH_SIZE]
        for recipient in batch:
            user_id = recipient["id"]
            email = recipient["email"]
            try:
                ok = send_email(email, subject, body_html, body_text)
            except Exception as exc:
                ok = False
                logger.warning(f"send_email raised for {email}: {exc}")

            async with _db_pool.acquire() as conn:
                if ok:
                    sent_count += 1
                    await conn.execute(
                        """
                        UPDATE email_broadcast_recipients
                        SET status = 'sent', sent_at = NOW()
                        WHERE broadcast_id = $1 AND user_id = $2
                        """,
                        broadcast_id,
                        user_id,
                    )
                else:
                    failed_count += 1
                    await conn.execute(
                        """
                        UPDATE email_broadcast_recipients
                        SET status = 'failed', error = 'send_email returned False'
                        WHERE broadcast_id = $1 AND user_id = $2
                        """,
                        broadcast_id,
                        user_id,
                    )

        if i + BATCH_SIZE < len(recipients):
            await asyncio.sleep(BATCH_DELAY_SECONDS)

    final_status = "failed" if sent_count == 0 and failed_count > 0 else "sent"
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE email_broadcasts SET status = $1, sent_at = NOW() WHERE id = $2",
            final_status,
            broadcast_id,
        )

    logger.info(
        f"Broadcast {broadcast_id} complete: {sent_count} sent, {failed_count} failed, "
        f"status={final_status}"
    )
