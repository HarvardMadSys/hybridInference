"""Persistence for batch processing jobs.

A batch is submitted through ``POST /v1/batches`` with an inline array of
requests. The gateway persists the job and its items here, then an in-process
worker drains pending items through the normal chat-completions path. This is a
small, self-contained store over the same Postgres pool used by the rest of the
gateway -- kept separate from the ``OperationalStore`` ABC (and from
``ResponseStore``) so neither contract is touched.

Two tables:

- ``batch_jobs``  -- one row per submitted batch (owner, status, counts).
- ``batch_items`` -- one row per request, with a nullable ``response``/``error``
  that is filled as the worker runs it.

When no database is configured the store is absent (``None`` in ``AppServices``)
and the batch surface degrades to 404, mirroring ``ResponseStore``.
"""

from __future__ import annotations

import json
from typing import Any

from serving.utils.logging import get_logger

logger = get_logger(__name__)

# Terminal statuses a batch can rest in. Values match the OpenAI Batch object
# vocabulary the router returns.
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "expired"})
ACTIVE_STATUSES = frozenset({"validating", "in_progress", "finalizing", "cancelling"})


class BatchStore:
    """Postgres-backed store for batch jobs and their items."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @property
    def pool(self) -> Any:
        """The asyncpg pool, for read-only helpers (e.g. the load gate)."""
        return self._pool

    async def initialize(self) -> None:
        """Create the batch tables and indexes (idempotent)."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS batch_jobs (
                    id              TEXT PRIMARY KEY,
                    user_id         TEXT NOT NULL,
                    role            TEXT NOT NULL DEFAULT 'free',
                    endpoint        TEXT NOT NULL,
                    status          TEXT NOT NULL,
                    model_ids       JSONB NOT NULL DEFAULT '[]'::jsonb,
                    request_count   INTEGER NOT NULL DEFAULT 0,
                    completed_count INTEGER NOT NULL DEFAULT 0,
                    failed_count    INTEGER NOT NULL DEFAULT 0,
                    metadata        JSONB,
                    callback_url    TEXT,
                    error           JSONB,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    started_at      TIMESTAMPTZ,
                    completed_at    TIMESTAMPTZ,
                    expires_at      TIMESTAMPTZ
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS batch_items (
                    id             BIGSERIAL PRIMARY KEY,
                    batch_id       TEXT NOT NULL
                                   REFERENCES batch_jobs(id) ON DELETE CASCADE,
                    custom_id      TEXT NOT NULL,
                    endpoint       TEXT NOT NULL,
                    model_id       TEXT NOT NULL,
                    request        JSONB NOT NULL,
                    response       JSONB,
                    error          JSONB,
                    status         TEXT NOT NULL DEFAULT 'queued',
                    attempts       INTEGER NOT NULL DEFAULT 0,
                    ttft_ms        INTEGER,
                    prompt_tokens  INTEGER,
                    completion_tokens INTEGER,
                    cost_usd       DECIMAL(14, 8),
                    next_attempt_at TIMESTAMPTZ,
                    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    completed_at   TIMESTAMPTZ,
                    UNIQUE (batch_id, custom_id)
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_batch_jobs_status ON batch_jobs(status)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_batch_jobs_user "
                "ON batch_jobs(user_id, created_at DESC)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_batch_items_pending "
                "ON batch_items(batch_id, status, id)"
            )

    async def create_batch(
        self,
        *,
        batch_id: str,
        user_id: str,
        role: str,
        endpoint: str,
        items: list[dict[str, Any]],
        model_ids: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Insert a batch and all of its items atomically."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                    INSERT INTO batch_jobs
                        (id, user_id, role, endpoint, status, model_ids,
                         request_count, metadata, expires_at)
                    VALUES ($1, $2, $3, $4, 'validating', $5::jsonb, $6, $7::jsonb,
                            NOW() + INTERVAL '24 hours')
                    """,
                batch_id,
                user_id,
                role,
                endpoint,
                json.dumps(model_ids),
                len(items),
                json.dumps(metadata) if metadata else None,
            )
            await conn.executemany(
                """
                    INSERT INTO batch_items
                        (batch_id, custom_id, endpoint, model_id, request)
                    VALUES ($1, $2, $3, $4, $5::jsonb)
                    """,
                [
                    (
                        batch_id,
                        item["custom_id"],
                        item.get("endpoint") or endpoint,
                        item["model_id"],
                        json.dumps(item["request"]),
                    )
                    for item in items
                ],
            )

    async def get_batch(
        self, batch_id: str, *, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """Fetch a batch row. ``user_id`` scopes it to its owner when given."""
        async with self._pool.acquire() as conn:
            if user_id is None:
                row = await conn.fetchrow("SELECT * FROM batch_jobs WHERE id = $1", batch_id)
            else:
                row = await conn.fetchrow(
                    "SELECT * FROM batch_jobs WHERE id = $1 AND user_id = $2",
                    batch_id,
                    user_id,
                )
        return _row_to_dict(row)

    async def list_batches(
        self, *, user_id: str, limit: int = 20, before: str | None = None
    ) -> list[dict[str, Any]]:
        """List a user's batches, newest first (OpenAI keyset style)."""
        async with self._pool.acquire() as conn:
            if before is None:
                rows = await conn.fetch(
                    "SELECT * FROM batch_jobs WHERE user_id = $1 "
                    "ORDER BY created_at DESC, id DESC LIMIT $2",
                    user_id,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM batch_jobs WHERE user_id = $1 AND id < $2 "
                    "ORDER BY created_at DESC, id DESC LIMIT $3",
                    user_id,
                    before,
                    limit,
                )
        return [_row_to_dict(r) for r in rows]

    async def get_items(self, batch_id: str) -> list[dict[str, Any]]:
        """Return every item of a batch, in submission order."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM batch_items WHERE batch_id = $1 ORDER BY id",
                batch_id,
            )
        return [_row_to_dict(r) for r in rows]

    async def delete_batch(self, batch_id: str, *, user_id: str | None = None) -> bool:
        """Purge a batch and (via cascade) all of its items."""
        async with self._pool.acquire() as conn:
            if user_id is None:
                result = await conn.execute("DELETE FROM batch_jobs WHERE id = $1", batch_id)
            else:
                result = await conn.execute(
                    "DELETE FROM batch_jobs WHERE id = $1 AND user_id = $2",
                    batch_id,
                    user_id,
                )
        return _rows_affected(result) > 0

    async def request_cancel(self, batch_id: str) -> None:
        """Ask the worker to stop launching new items for a batch."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE batch_jobs SET status = 'cancelling' "
                "WHERE id = $1 AND status NOT IN ('completed','failed','cancelled','expired')",
                batch_id,
            )

    async def set_status(
        self,
        batch_id: str,
        status: str,
        *,
        error: dict[str, Any] | None = None,
    ) -> None:
        """Set a batch's status, stamping the terminal timestamp when reached."""
        terminal = status in TERMINAL_STATUSES
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE batch_jobs SET
                    status = $2,
                    error = COALESCE($3::jsonb, error),
                    started_at = CASE
                        WHEN started_at IS NULL AND $2 = 'in_progress' THEN NOW()
                        ELSE started_at END,
                    completed_at = CASE
                        WHEN completed_at IS NULL AND $4 THEN NOW()
                        ELSE completed_at END
                WHERE id = $1
                """,
                batch_id,
                status,
                json.dumps(error) if error else None,
                terminal,
            )

    async def fetch_runnable_items(self, batch_id: str, limit: int) -> list[dict[str, Any]]:
        """Return up to *limit* queued items whose backoff has elapsed."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM batch_items
                WHERE batch_id = $1
                  AND status = 'queued'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
                ORDER BY id
                LIMIT $2
                """,
                batch_id,
                limit,
            )
        return [_row_to_dict(r) for r in rows]

    async def mark_item_running(self, item_id: int) -> None:
        """Mark an item as running and bump its attempt count."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE batch_items SET status = 'running', attempts = attempts + 1 WHERE id = $1",
                item_id,
            )

    async def mark_item_done(
        self,
        item_id: int,
        *,
        response: dict[str, Any] | None,
        error: dict[str, Any] | None = None,
        ttft_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        """Record an item's terminal result (a response, or an error)."""
        status = "failed" if error is not None else "completed"
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE batch_items SET
                    status = $2,
                    response = $3::jsonb,
                    error = $4::jsonb,
                    ttft_ms = $5,
                    prompt_tokens = $6,
                    completion_tokens = $7,
                    cost_usd = $8,
                    completed_at = NOW()
                WHERE id = $1
                """,
                item_id,
                status,
                json.dumps(response) if response is not None else None,
                json.dumps(error) if error is not None else None,
                ttft_ms,
                prompt_tokens,
                completion_tokens,
                cost_usd,
            )

    async def requeue_item(self, item_id: int, *, next_attempt_at: Any = None) -> None:
        """Return a running item to the queue after a transient failure."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE batch_items SET status = 'queued', next_attempt_at = $2
                WHERE id = $1
                """,
                item_id,
                next_attempt_at,
            )

    async def refresh_counts(self, batch_id: str) -> dict[str, Any]:
        """Recompute item counts, then advance the batch status when settled."""
        async with self._pool.acquire() as conn:
            counts = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'completed') AS completed,
                    COUNT(*) FILTER (WHERE status = 'failed')    AS failed,
                    COUNT(*) FILTER (WHERE status NOT IN
                        ('completed','failed'))                  AS open
                FROM batch_items WHERE batch_id = $1
                """,
                batch_id,
            )
            await conn.execute(
                "UPDATE batch_jobs SET completed_count = $2, failed_count = $3 WHERE id = $1",
                batch_id,
                counts["completed"],
                counts["failed"],
            )
        return {
            "completed": counts["completed"],
            "failed": counts["failed"],
            "open": counts["open"],
        }

    async def list_active_batches(self, limit: int = 50) -> list[dict[str, Any]]:
        """Batches still eligible for work, oldest first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM batch_jobs
                WHERE status IN ('validating', 'in_progress', 'finalizing', 'cancelling')
                ORDER BY created_at
                LIMIT $1
                """,
                limit,
            )
        return [_row_to_dict(r) for r in rows]

    async def cancel_pending_items(self, batch_id: str) -> None:
        """Mark a cancelled batch's not-yet-finished items as cancelled."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE batch_items SET status = 'cancelled' "
                "WHERE batch_id = $1 AND status IN ('queued', 'running')",
                batch_id,
            )


def _rows_affected(result: str) -> int:
    try:
        return int(result.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("model_ids", "metadata", "error", "request", "response"):
        if key in out:
            out[key] = _load_json(out[key])
    return out


def _load_json(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Failed to decode batch JSON column", exc_info=True)
            return None
    return value
