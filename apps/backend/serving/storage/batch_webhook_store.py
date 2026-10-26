"""Batch completion webhooks: per-account settings and the event outbox.

A user configures at most one webhook (``batch_webhooks``). When one of their
batches is sealed, the sealing transaction writes an event to
``batch_webhook_events`` (see ``serving.storage.batch_store._enqueue_event``),
so an event exists exactly when the batch's results do, and never before. The
delivery worker (:mod:`serving.batch.webhooks`) leases due events, posts them,
and records the outcome: delivered, retried later with backoff, or given up
after the last attempt. Delivery is at least once; receivers deduplicate on
the event id, which every attempt repeats.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from serving.storage.batch_store import _CLAIM_LOCK_SQL, _USER_ADMISSION_LOCK_CLASS, _rowcount

if TYPE_CHECKING:
    from datetime import datetime


class BatchWebhookStore:
    """Webhook settings and the outbox, over the batch store's pool."""

    def __init__(self, pool: Any) -> None:
        """Wrap the asyncpg pool the batch store uses."""
        self._pool = pool

    async def get_webhook(self, user_id: str) -> dict[str, Any] | None:
        """A user's webhook row (``url``, ``secret_encrypted``, ``enabled``, times)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id, url, secret_encrypted, enabled, created_at, updated_at "
                "FROM batch_webhooks WHERE user_id = $1",
                user_id,
            )
        return dict(row) if row is not None else None

    async def set_webhook(
        self,
        *,
        user_id: str,
        url: str,
        secret_encrypted: str | None,
        enabled: bool,
        check_owner: bool = False,
    ) -> dict[str, Any]:
        """Create or update a user's webhook; ``secret_encrypted`` None keeps the stored one.

        With ``check_owner``, the account must still be active and not being
        erased when the write happens -- a request that authenticated before a
        permanent deletion cannot leave a webhook behind it (the deletion takes
        the same per-user lock).

        Raises:
            LookupError: There is no webhook yet and no secret was given, or
                the owner is gone.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(_CLAIM_LOCK_SQL, _USER_ADMISSION_LOCK_CLASS, user_id)
            if check_owner:
                owner = await conn.fetchrow(
                    "SELECT status, hard_delete_pending FROM users WHERE id = $1 FOR SHARE",
                    user_id,
                )
                if owner is None or owner["status"] != "active" or owner["hard_delete_pending"]:
                    raise LookupError(user_id)
            if secret_encrypted is None:
                row = await conn.fetchrow(
                    "UPDATE batch_webhooks SET url = $2, enabled = $3, updated_at = now() "
                    "WHERE user_id = $1 "
                    "RETURNING user_id, url, secret_encrypted, enabled, created_at, updated_at",
                    user_id,
                    url,
                    enabled,
                )
                if row is None:
                    raise LookupError(user_id)
            else:
                row = await conn.fetchrow(
                    """
                    INSERT INTO batch_webhooks (user_id, url, secret_encrypted, enabled)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (user_id) DO UPDATE SET
                        url = EXCLUDED.url, secret_encrypted = EXCLUDED.secret_encrypted,
                        enabled = EXCLUDED.enabled, updated_at = now()
                    RETURNING user_id, url, secret_encrypted, enabled, created_at, updated_at
                    """,
                    user_id,
                    url,
                    secret_encrypted,
                    enabled,
                )
        return dict(row)

    async def delete_webhook(self, user_id: str) -> bool:
        """Remove a user's webhook and its undelivered events. True when one existed.

        The webhook goes first: deleting its row waits for any sealing
        transaction that is writing an event under it (it holds the row ``FOR
        KEY SHARE``), and the event delete that follows, a new statement, sees
        that event once it is committed.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            result = await conn.execute("DELETE FROM batch_webhooks WHERE user_id = $1", user_id)
            await conn.execute(
                "DELETE FROM batch_webhook_events WHERE user_id = $1 AND status = 'pending'",
                user_id,
            )
        return _rowcount(result) > 0

    async def list_events(self, user_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """A user's most recent events with their delivery state (no payload secrets)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, batch_id, event_type, status, attempts, last_status_code, "
                "last_error, created_at, delivered_at, next_attempt_at "
                "FROM batch_webhook_events WHERE user_id = $1 "
                "ORDER BY created_at DESC LIMIT $2",
                user_id,
                limit,
            )
        return [dict(row) for row in rows]

    async def claim_due_events(
        self, *, worker_id: str, limit: int, lease_seconds: int, max_attempts: int = 8
    ) -> list[dict[str, Any]]:
        """Lease due events (with their webhook's URL and secret) for delivery.

        Each lease counts as an attempt; an event out of attempts (its last
        holder died after taking it) is given up by :meth:`fail_exhausted`.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                """
                UPDATE batch_webhook_events AS e
                SET lease_owner = $1,
                    lease_expires_at = now() + make_interval(secs => $2),
                    attempts = e.attempts + 1
                FROM batch_webhooks AS w
                WHERE e.id IN (
                    -- Only events whose webhook still exists: one left without
                    -- it must never take the place of a deliverable event.
                    SELECT pending.id FROM batch_webhook_events AS pending
                    JOIN batch_webhooks AS hook ON hook.user_id = pending.user_id
                    WHERE pending.status = 'pending' AND pending.next_attempt_at <= now()
                      AND pending.attempts < $4
                      AND (pending.lease_expires_at IS NULL OR pending.lease_expires_at <= now())
                    ORDER BY pending.next_attempt_at, pending.id
                    LIMIT $3
                    FOR UPDATE OF pending SKIP LOCKED
                )
                  AND w.user_id = e.user_id
                RETURNING e.id, e.user_id, e.batch_id, e.event_type, e.payload, e.attempts,
                          e.created_at, w.url, w.secret_encrypted, w.enabled
                """,
                worker_id,
                float(lease_seconds),
                limit,
                max_attempts,
            )
        return [dict(row) for row in rows]

    async def mark_delivered(
        self, *, event_id: str, worker_id: str, attempt: int, status_code: int
    ) -> bool:
        """Record a delivery the receiver acknowledged (fenced on this lease and attempt)."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE batch_webhook_events SET status = 'delivered', delivered_at = now(), "
                "last_status_code = $4, last_error = NULL, lease_owner = NULL, "
                "lease_expires_at = NULL WHERE id = $1 AND lease_owner = $2 AND attempts = $3 "
                "AND status = 'pending'",
                event_id,
                worker_id,
                attempt,
                status_code,
            )
        return _rowcount(result) > 0

    async def mark_failed(
        self,
        *,
        event_id: str,
        worker_id: str,
        attempt: int,
        error: str,
        status_code: int | None,
        next_attempt_at: datetime | None,
    ) -> bool:
        """Record a failed delivery: retried at ``next_attempt_at``, or given up when None."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE batch_webhook_events
                SET status = CASE WHEN $6::timestamptz IS NULL THEN 'failed' ELSE 'pending' END,
                    next_attempt_at = COALESCE($6::timestamptz, next_attempt_at),
                    last_error = $4, last_status_code = $5,
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE id = $1 AND lease_owner = $2 AND attempts = $3 AND status = 'pending'
                """,
                event_id,
                worker_id,
                attempt,
                error[:500],
                status_code,
                next_attempt_at,
            )
        return _rowcount(result) > 0

    async def fail_exhausted(self, *, max_attempts: int = 8) -> int:
        """Give up events that can no longer be delivered.

        Those whose every attempt was taken and whose last lease ran out, and
        those whose webhook no longer exists (left by an older version, which
        removed events before the webhook).
        """
        async with self._pool.acquire() as conn:
            exhausted = await conn.execute(
                "UPDATE batch_webhook_events SET status = 'failed', "
                "last_error = COALESCE(last_error, 'delivery attempts exhausted') "
                "WHERE status = 'pending' AND attempts >= $1 "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= now())",
                max_attempts,
            )
            orphaned = await conn.execute(
                "UPDATE batch_webhook_events AS e SET status = 'failed', "
                "last_error = 'webhook removed', lease_owner = NULL, lease_expires_at = NULL "
                "WHERE e.status = 'pending' "
                "AND NOT EXISTS (SELECT 1 FROM batch_webhooks AS w WHERE w.user_id = e.user_id)"
            )
        return _rowcount(exhausted) + _rowcount(orphaned)

    async def purge_old_events(self, *, days: int = 30) -> int:
        """Remove events finished more than *days* ago."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM batch_webhook_events WHERE status <> 'pending' "
                "AND created_at < now() - make_interval(days => $1)",
                days,
            )
        return _rowcount(result)
