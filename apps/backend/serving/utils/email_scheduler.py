"""APScheduler-backed broadcast email scheduler."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from apscheduler.jobstores.base import JobLookupError
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
    # All scheduled_at timestamps are stored as TIMESTAMPTZ and rehydrated
    # with tz=UTC; pin the scheduler to UTC so DateTrigger interprets them
    # consistently regardless of the host's local timezone.
    _scheduler = AsyncIOScheduler(timezone=timezone.utc)
    _scheduler.start()
    logger.info("Broadcast email scheduler started")


def stop_scheduler() -> None:
    """Shut down APScheduler gracefully."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Broadcast email scheduler stopped")
    _scheduler = None


def get_scheduler() -> AsyncIOScheduler | None:
    """Return the live AsyncIOScheduler, or None if not started.

    Allows other modules (e.g., provider-stats rollup) to register additional
    jobs on the same scheduler.
    """
    return _scheduler


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
    """Remove a scheduled APScheduler job, no-op if the job is unknown.

    The job may not exist locally because a different replica owns it, or
    because it has already fired.
    """
    if not _scheduler:
        return
    try:
        _scheduler.remove_job(broadcast_id)
    except JobLookupError:
        logger.debug(f"cancel_broadcast_job: no APScheduler job for {broadcast_id}")


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
    """Send emails for a broadcast using a pre-snapshotted recipient list.

    Atomically claims the broadcast via UPDATE...RETURNING so concurrent replicas
    cannot double-send. SMTP I/O is dispatched to a thread pool so it doesn't
    block the event loop.
    """
    if not _db_pool:
        logger.error(f"Cannot execute broadcast {broadcast_id}: no DB pool")
        return

    # Atomically claim — only one replica wins, even on rehydration races.
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE email_broadcasts
            SET status = 'sending'
            WHERE id = $1 AND status = 'scheduled'
            RETURNING id, subject, body_html, body_text
            """,
            broadcast_id,
        )

    if not row:
        logger.info(
            f"Broadcast {broadcast_id} not in 'scheduled' state — already claimed or not found"
        )
        return

    subject = row["subject"]
    body_html = row["body_html"]
    body_text = row["body_text"]

    # Recipients were snapshotted at create time.
    async with _db_pool.acquire() as conn:
        recipients = await conn.fetch(
            """
            SELECT user_id, email FROM email_broadcast_recipients
            WHERE broadcast_id = $1 AND status = 'pending'
            ORDER BY id
            """,
            broadcast_id,
        )

    sent_count = 0
    failed_count = 0

    for i in range(0, len(recipients), BATCH_SIZE):
        batch = recipients[i : i + BATCH_SIZE]

        # SMTP is sync; offload to a thread so it doesn't block the event loop.
        sent_user_ids: list[str] = []
        # (user_id, error_message) pairs so we can persist real diagnostics.
        failed_results: list[tuple[str, str]] = []
        for recipient in batch:
            user_id = recipient["user_id"]
            email = recipient["email"]
            error_msg: str | None = None
            try:
                ok = await asyncio.to_thread(send_email, email, subject, body_html, body_text)
                if not ok:
                    error_msg = "send_email returned False (SMTP send unsuccessful)"
            except Exception as exc:
                ok = False
                error_msg = f"{type(exc).__name__}: {exc}"[:500]
                logger.warning(f"send_email raised for {email}: {exc}")
            if ok:
                sent_user_ids.append(user_id)
            else:
                failed_results.append((user_id, error_msg or "unknown error"))

        # One connection acquisition per batch, not per recipient.
        async with _db_pool.acquire() as conn:
            if sent_user_ids:
                await conn.execute(
                    """
                    UPDATE email_broadcast_recipients
                    SET status = 'sent', sent_at = NOW()
                    WHERE broadcast_id = $1 AND user_id = ANY($2::text[])
                    """,
                    broadcast_id,
                    sent_user_ids,
                )
                sent_count += len(sent_user_ids)
            if failed_results:
                # Single-statement bulk update with per-user error messages, joined
                # via UNNEST so we don't N round-trip the DB per failed recipient.
                await conn.execute(
                    """
                    UPDATE email_broadcast_recipients AS r
                    SET status = 'failed', error = u.err
                    FROM UNNEST($2::text[], $3::text[]) AS u(uid, err)
                    WHERE r.broadcast_id = $1 AND r.user_id = u.uid
                    """,
                    broadcast_id,
                    [uid for uid, _ in failed_results],
                    [err for _, err in failed_results],
                )
                failed_count += len(failed_results)

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
