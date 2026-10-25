"""Persistence for batch inference (``/v1/batches``).

A batch is one row of ``batch_jobs`` plus one row of ``batch_items`` per
request; ``batch_model_limits`` holds the per-model concurrency caps an
administrator sets. Like :mod:`serving.storage.responses_store` this is a small,
self-contained store over the shared Postgres pool, outside the
``OperationalStore`` contract.

Every gateway process runs a worker (:mod:`serving.batch.scheduler`) that claims
items from here, so whatever coordinates the processes lives in SQL:

- **Claims** for one model are serialized across processes by a
  transaction-scoped advisory lock keyed on the model, and count the model's
  live leases while holding it, so a model's cap holds deployment-wide. Batches
  take turns: the one claimed from least recently goes first.
- **Leases**: a claimed item stays ``running`` until ``lease_expires_at``, which
  its worker keeps extending (:meth:`BatchStore.heartbeat`). The item of a
  worker that died is claimable again once its lease has run out, and that
  attempt counts.
- **Guards**: a worker's write (complete, retry, release) applies only while
  the item is still running under that worker's claim and attempt, so a lost
  lease or a deleted batch makes it a no-op (``False``) instead of overwriting
  newer state.
- **Settling** (``_settle``) runs in the transaction of every item and batch
  transition: a ``cancelling``, ``failed`` or ``expired`` batch passes its
  verdict on to its queued items (and to running items whose lease ran out,
  which no worker can claim back), and a batch with no open item left becomes
  ``completed``, or ``cancelled``. A late completion or retry therefore never
  undoes a cancel.
- **Lock order**: claims, heartbeats and user deletes take model advisory locks
  in sorted order before row locks. Worker writes lock a batch before its item,
  and several batch rows in id order. No transaction reverses a shared lock
  order, so two transactions cannot wait on each other in a cycle.

Schema creation follows :mod:`serving.storage.log_schema`: the catalog is read
first, so a settled database takes no DDL lock at all, and whatever is missing
is created in one transaction under the bounded lock wait. An advisory lock
serializes the gateway processes that boot at once, since two concurrent
``CREATE TABLE IF NOT EXISTS`` can still collide on the catalog's unique index.
"""

from __future__ import annotations

import base64
import json
import math
from typing import TYPE_CHECKING, Any

import asyncpg

from serving.storage.log_schema import bounded_ddl, execute_ddl
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from datetime import datetime

logger = get_logger(__name__)


class BatchOwnerUnavailable(RuntimeError):
    """An authenticated batch owner is no longer allowed to create work."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        super().__init__(f"Batch owner {user_id!r} is not an active account.")


class BatchDeleteInProgress(RuntimeError):
    """A batch cannot be deleted while one of its upstream calls is live."""

    def __init__(self, batch_id: str) -> None:
        self.batch_id = batch_id
        super().__init__(f"Batch {batch_id!r} still has a running request.")


#: A batch in one of these statuses is finished: its status never changes again
#: (items already running when it expired or failed may still finish).
TERMINAL_BATCH_STATUSES: frozenset[str] = frozenset({"completed", "failed", "expired", "cancelled"})
#: Batches in these statuses count toward a user's cap on active batches.
ACTIVE_BATCH_STATUSES: frozenset[str] = frozenset({"validating", "in_progress", "cancelling"})

# Two-key advisory locks: this class plus ``hashtext(model_id)`` serializes the
# claims for one model across every process (two models sharing a hash merely
# claim one after the other). The two-key space is separate from the one-key
# space the other locks in this layer use.
_CLAIM_LOCK_CLASS = 0x42415443  # ASCII "BATC"
_CLAIM_LOCK_SQL = "SELECT pg_advisory_xact_lock($1, hashtext($2))"

# Serializes schema creation across processes booting at once.
_SCHEMA_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('hybridinference:batch-schema'))"

# Retention purges at most this many batches per sweep, one per statement: a
# batch can hold tens of thousands of items, and the next sweep continues.
_PURGE_LIMIT = 100

# JSONB rejects U+0000 even when it appears as a JSON ``\u0000`` escape. Values
# that contain one are therefore stored as a two-element JSON array carrying a
# marker and base64 of the original JSON text. All persisted batch payloads are
# objects, so the array cannot collide with their ordinary JSONB shape. The
# helper also escapes an exact envelope-shaped array if called outside that
# narrower storage contract.
_JSON_ENVELOPE_MARKER = "hybridinference:batch-json:v1"

_TABLES: tuple[tuple[str, str], ...] = (
    (
        "batch_jobs",
        """
        CREATE TABLE IF NOT EXISTS batch_jobs (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            key_hash TEXT,
            endpoint TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'validating',
            model_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            request_count INTEGER NOT NULL DEFAULT 0,
            completed_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            metadata JSONB,
            error JSONB,
            cost_usd NUMERIC(18, 8) NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            in_progress_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            failed_at TIMESTAMPTZ,
            expired_at TIMESTAMPTZ,
            cancelling_at TIMESTAMPTZ,
            cancelled_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ NOT NULL,
            paused_until TIMESTAMPTZ,
            last_claimed_at TIMESTAMPTZ
        )
        """,
    ),
    (
        "batch_items",
        """
        CREATE TABLE IF NOT EXISTS batch_items (
            id BIGSERIAL PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES batch_jobs(id) ON DELETE CASCADE,
            custom_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            request JSONB NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ,
            claimed_by TEXT,
            lease_expires_at TIMESTAMPTZ,
            response JSONB,
            error JSONB,
            status_code INTEGER,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            cost_usd NUMERIC(14, 8),
            request_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            CONSTRAINT batch_items_custom_id_key UNIQUE (batch_id, custom_id)
        )
        """,
    ),
    (
        "batch_model_limits",
        """
        CREATE TABLE IF NOT EXISTS batch_model_limits (
            model_id TEXT PRIMARY KEY,
            max_concurrency INTEGER NOT NULL CHECK (max_concurrency >= 0),
            updated_by TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
    ),
)

# ``(name, ddl)`` per index, created after the tables.
_INDEXES: tuple[tuple[str, str], ...] = (
    (
        # A user's batches, newest first (list, active count, pause, purge).
        "idx_batch_jobs_user",
        "CREATE INDEX IF NOT EXISTS idx_batch_jobs_user "
        "ON batch_jobs (user_id, created_at DESC, id DESC)",
    ),
    (
        # The few active batches among days of finished ones: what the worker
        # claims from, expires and settles.
        "idx_batch_jobs_active",
        "CREATE INDEX IF NOT EXISTS idx_batch_jobs_active ON batch_jobs (expires_at) "
        "WHERE status IN ('validating', 'in_progress', 'cancelling')",
    ),
    (
        # Finished batches by the time they finished, for the retention purge.
        # The expression must stay identical to the purge query's.
        "idx_batch_jobs_finished",
        "CREATE INDEX IF NOT EXISTS idx_batch_jobs_finished "
        "ON batch_jobs ((COALESCE(completed_at, failed_at, expired_at, cancelled_at))) "
        "WHERE status IN ('completed', 'failed', 'expired', 'cancelled')",
    ),
    (
        # Results paging, in submission order.
        "idx_batch_items_batch",
        "CREATE INDEX IF NOT EXISTS idx_batch_items_batch ON batch_items (batch_id, id)",
    ),
    (
        # Open items only: a batch's next items for one model in id order (the
        # claim), and whether a batch has any left (settling). Finished items
        # leave it, so it stays the size of the backlog.
        "idx_batch_items_open",
        "CREATE INDEX IF NOT EXISTS idx_batch_items_open "
        "ON batch_items (batch_id, model_id, id) WHERE status IN ('queued', 'running')",
    ),
    (
        # A model's running items, so counting its live leases never reads
        # its queued backlog.
        "idx_batch_items_running",
        "CREATE INDEX IF NOT EXISTS idx_batch_items_running "
        "ON batch_items (model_id, lease_expires_at) WHERE status = 'running'",
    ),
)

_BATCH_COLUMNS = (
    "id, user_id, key_hash, endpoint, status, model_ids, request_count, completed_count, "
    "failed_count, metadata, error, cost_usd, created_at, in_progress_at, completed_at, "
    "failed_at, expired_at, cancelling_at, cancelled_at, expires_at, paused_until"
)
_RESULT_COLUMNS = (
    "id, custom_id, status, status_code, response, error, prompt_tokens, completion_tokens, "
    "cost_usd, request_id, completed_at"
)
# Filled by COPY in this order; every other column takes its default, and
# ``id`` follows the order the rows arrive in.
_ITEM_COPY_COLUMNS = ("batch_id", "custom_id", "model_id", "request")

# Candidates for one claim: the batches whose items may be claimed now that hold
# the model (least recently claimed first), each contributing its claimable
# items in id order, cut to the room. Read without locks; the claim re-checks.
_CLAIM_CANDIDATES_SQL = """
    SELECT i.id, i.batch_id
    FROM (
        SELECT id, last_claimed_at, created_at
        FROM batch_jobs
        WHERE status IN ('validating', 'in_progress')
          AND (paused_until IS NULL OR paused_until <= now())
          AND expires_at > now()
          AND model_ids ? $1
    ) AS b
    CROSS JOIN LATERAL (
        SELECT bi.id, bi.batch_id
        FROM batch_items AS bi
        WHERE bi.batch_id = b.id
          AND bi.model_id = $1
          AND bi.status IN ('queued', 'running')
          AND (
              (bi.status = 'queued' AND (bi.next_attempt_at IS NULL OR bi.next_attempt_at <= now()))
              OR (bi.status = 'running' AND bi.lease_expires_at <= now())
          )
        ORDER BY bi.id
        LIMIT $2
    ) AS i
    ORDER BY b.last_claimed_at ASC NULLS FIRST, b.created_at ASC, b.id ASC, i.id ASC
    LIMIT $2
"""

# Locks the candidates' batches in id order, keeping those still claimable.
_CLAIM_LOCK_BATCHES_SQL = """
    SELECT id, user_id, key_hash
    FROM batch_jobs
    WHERE id = ANY($1::text[])
      AND status IN ('validating', 'in_progress')
      AND (paused_until IS NULL OR paused_until <= now())
      AND expires_at > now()
    ORDER BY id
    FOR NO KEY UPDATE
"""

_CLAIM_ITEMS_SQL = """
    UPDATE batch_items
    SET status = 'running',
        attempts = attempts + 1,
        claimed_by = $2,
        lease_expires_at = now() + make_interval(secs => $3),
        started_at = COALESCE(started_at, now()),
        next_attempt_at = NULL
    WHERE id = ANY($1::bigint[])
      AND batch_id = ANY($4::text[])
      AND model_id = $5
      AND (
          (status = 'queued' AND (next_attempt_at IS NULL OR next_attempt_at <= now()))
          OR (status = 'running' AND lease_expires_at <= now())
      )
    RETURNING id, batch_id, custom_id, model_id, request, attempts, error
"""

_CLAIM_TOUCH_BATCHES_SQL = """
    UPDATE batch_jobs
    SET last_claimed_at = now(),
        in_progress_at = CASE WHEN status = 'validating' THEN now() ELSE in_progress_at END,
        status = CASE WHEN status = 'validating' THEN 'in_progress' ELSE status END
    WHERE id = ANY($1::text[])
"""

# The batch of an item, locked before the item itself (see the lock order).
_LOCK_ITEM_BATCH_SQL = """
    SELECT id, status
    FROM batch_jobs
    WHERE id = (SELECT batch_id FROM batch_items WHERE id = $1)
    FOR NO KEY UPDATE
"""

# The guard every worker write carries: the item is still running under this
# worker's claim, at this attempt.
_ITEM_GUARD = "id = $1 AND status = 'running' AND claimed_by = $2 AND attempts = $3"

_COMPLETE_ITEM_SQL = f"""
    UPDATE batch_items
    SET status = $4, response = $5::jsonb, error = $6::jsonb, status_code = $7,
        prompt_tokens = $8, completion_tokens = $9, cost_usd = $10, request_id = $11,
        completed_at = now(), lease_expires_at = NULL, next_attempt_at = NULL
    WHERE {_ITEM_GUARD}
"""

_RETRY_ITEM_SQL = f"""
    UPDATE batch_items
    SET status = 'queued', next_attempt_at = $4, error = $5::jsonb,
        claimed_by = NULL, lease_expires_at = NULL
    WHERE {_ITEM_GUARD}
"""

_RELEASE_ITEM_SQL = f"""
    UPDATE batch_items
    SET status = 'queued', attempts = GREATEST(attempts - 1, 0), next_attempt_at = $4,
        claimed_by = NULL, lease_expires_at = NULL
    WHERE {_ITEM_GUARD}
"""

# What the open items of a batch in each of these statuses become when it
# settles: the item status, and the error recorded (None keeps the item's own).
_ITEM_VERDICTS: dict[str, tuple[str, dict[str, str] | None]] = {
    "cancelling": ("cancelled", None),
    "failed": (
        "failed",
        {"code": "batch_failed", "message": "The batch failed before this request ran."},
    ),
    "expired": (
        "expired",
        {
            "code": "batch_expired",
            "message": "The batch's completion window ended before this request ran.",
        },
    ),
}

# Queued items, and running ones whose lease has run out: in a batch that can no
# longer be claimed from, nothing would ever pick those up again.
_SETTLE_ITEMS_SQL = """
    UPDATE batch_items
    SET status = $2, error = COALESCE($3::jsonb, error), completed_at = now(),
        next_attempt_at = NULL, lease_expires_at = NULL
    WHERE batch_id = $1
      AND (status = 'queued' OR (status = 'running' AND lease_expires_at <= now()))
"""

_HAS_OPEN_ITEMS_SQL = (
    "SELECT EXISTS (SELECT 1 FROM batch_items "
    "WHERE batch_id = $1 AND status IN ('queued', 'running'))"
)

# The status a batch takes once no open item is left, by its current status.
_FINALIZE_SQL: dict[str, tuple[str, str]] = {
    "validating": (
        "completed",
        "UPDATE batch_jobs SET status = 'completed', completed_at = now() WHERE id = $1",
    ),
    "in_progress": (
        "completed",
        "UPDATE batch_jobs SET status = 'completed', completed_at = now() WHERE id = $1",
    ),
    "cancelling": (
        "cancelled",
        "UPDATE batch_jobs SET status = 'cancelled', cancelled_at = now() WHERE id = $1",
    ),
}

# Batches a sweep settles: active ones with no open item left, and those that
# can no longer be claimed from but hold a running item whose lease ran out.
_SWEEP_SETTLE_CANDIDATES_SQL = """
    SELECT j.id
    FROM batch_jobs AS j
    WHERE j.status IN ('validating', 'in_progress', 'cancelling')
      AND NOT EXISTS (
          SELECT 1 FROM batch_items AS i
          WHERE i.batch_id = j.id AND i.status IN ('queued', 'running')
      )
    UNION
    SELECT i.batch_id
    FROM batch_items AS i
    JOIN batch_jobs AS j ON j.id = i.batch_id
    WHERE i.status = 'running'
      AND i.lease_expires_at <= now()
      AND j.status IN ('cancelling', 'failed', 'expired')
"""

# One finished batch past retention, oldest first; a batch another sweep is
# deleting is skipped rather than waited for.
_PURGE_ONE_SQL = """
    DELETE FROM batch_jobs
    WHERE id = (
        SELECT id FROM batch_jobs
        WHERE status IN ('completed', 'failed', 'expired', 'cancelled')
          AND COALESCE(completed_at, failed_at, expired_at, cancelled_at)
              < now() - make_interval(days => $1)
        ORDER BY COALESCE(completed_at, failed_at, expired_at, cancelled_at)
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id
"""


class BatchStore:
    """Postgres-backed store for batch jobs, their items and per-model caps."""

    def __init__(self, pool: Any) -> None:
        """Wrap an asyncpg pool."""
        self._pool = pool

    async def initialize(self) -> None:
        """Create the batch tables and their indexes (idempotent).

        Reads the catalog first, so a settled database takes no lock at all.
        Whatever is missing is created in one transaction, under an advisory
        lock that serializes the processes booting at once and under the
        bounded lock wait of :mod:`serving.storage.log_schema`.

        Raises:
            SchemaLockUnavailable: An index on an existing table could not take
                its lock in time; nothing changed, retry later.
        """
        async with self._pool.acquire() as conn:
            if not await _missing_schema_objects(conn):
                return
            async with bounded_ddl(conn), conn.transaction():
                await execute_ddl(conn, _SCHEMA_LOCK_SQL)
                # Another process may have created everything while this one
                # waited for the lock.
                missing = await _missing_schema_objects(conn)
                for name, ddl in (*_TABLES, *_INDEXES):
                    if name in missing:
                        await execute_ddl(conn, ddl)

    # -- API surface -----------------------------------------------------------

    async def create_batch(
        self,
        *,
        batch_id: str,
        user_id: str,
        key_hash: str | None,
        endpoint: str,
        model_ids: list[str],
        items: list[dict[str, Any]],
        metadata: dict[str, Any] | None,
        completion_window_seconds: int,
    ) -> dict[str, Any]:
        """Store a new batch and all of its items in one transaction.

        Args:
            batch_id: The batch id.
            user_id: Its owner.
            key_hash: HMAC of the API key that created it (None when user auth
                is disabled); the worker re-authenticates it before each item.
            endpoint: The target endpoint, e.g. ``/v1/chat/completions``.
            model_ids: The models the items use. A model an item names but
                this list lacks is added, so no item can be stranded.
            items: ``{"custom_id", "model_id", "request"}`` per request, in
                submission order; item ids follow it.
            metadata: The caller's metadata, or None.
            completion_window_seconds: Time the batch has before its
                unstarted items expire.

        Returns:
            The batch row, status ``validating``.

        Raises:
            BatchOwnerUnavailable: An authenticated owner no longer exists,
                is not active, or has a hard-delete in progress.
            ValueError: Two items share a ``custom_id``, or one holds a NUL
                character (which a text column cannot store).
        """
        seen: set[str] = set()
        for item in items:
            custom_id = item["custom_id"]
            if custom_id in seen:
                raise ValueError(f"custom_id {custom_id!r} appears more than once")
            if "\x00" in custom_id:
                raise ValueError("custom_id must not contain a NUL character")
            seen.add(custom_id)
        models = list(dict.fromkeys([*model_ids, *(item["model_id"] for item in items)]))
        records = [
            (batch_id, item["custom_id"], item["model_id"], _dump_json(item["request"]))
            for item in items
        ]
        async with self._pool.acquire() as conn, conn.transaction():
            if key_hash is not None:
                owner = await conn.fetchrow(
                    "SELECT status, hard_delete_pending FROM users WHERE id = $1 FOR SHARE",
                    user_id,
                )
                if owner is None or owner["status"] != "active" or owner["hard_delete_pending"]:
                    raise BatchOwnerUnavailable(user_id)
            row = await conn.fetchrow(
                f"""
                INSERT INTO batch_jobs
                    (id, user_id, key_hash, endpoint, model_ids, request_count,
                     metadata, expires_at)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb,
                        now() + make_interval(secs => $8))
                RETURNING {_BATCH_COLUMNS}
                """,
                batch_id,
                user_id,
                key_hash,
                endpoint,
                json.dumps(models),
                len(items),
                _dump_json(metadata),
                float(completion_window_seconds),
            )
            if records:
                await conn.copy_records_to_table(
                    "batch_items", records=records, columns=_ITEM_COPY_COLUMNS
                )
        return _batch_row(row)

    async def count_active_batches(self, user_id: str) -> int:
        """Count a user's batches that are validating, in progress or cancelling."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT count(*) FROM batch_jobs WHERE user_id = $1 "
                "AND status IN ('validating', 'in_progress', 'cancelling')",
                user_id,
            )

    async def get_batch(
        self, batch_id: str, *, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """Fetch a batch row, or None when absent (or not *user_id*'s, when given)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_BATCH_COLUMNS} FROM batch_jobs "
                "WHERE id = $1 AND ($2::text IS NULL OR user_id = $2)",
                batch_id,
                user_id,
            )
        return _batch_row(row) if row is not None else None

    async def list_batches(
        self, *, user_id: str, limit: int, after: str | None = None
    ) -> list[dict[str, Any]]:
        """List a user's batches, newest first.

        Args:
            user_id: The owner.
            limit: Page size.
            after: Id of the last batch of the previous page. One that does
                not exist (any more), or is someone else's, ends the listing.

        Returns:
            Up to ``limit`` batch rows ordered by ``created_at DESC, id DESC``.
        """
        async with self._pool.acquire() as conn:
            if after is None:
                rows = await conn.fetch(
                    f"SELECT {_BATCH_COLUMNS} FROM batch_jobs WHERE user_id = $1 "
                    "ORDER BY created_at DESC, id DESC LIMIT $2",
                    user_id,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    f"""
                    SELECT {_BATCH_COLUMNS} FROM batch_jobs
                    WHERE user_id = $1
                      AND (created_at, id) < (
                          SELECT created_at, id FROM batch_jobs WHERE id = $3 AND user_id = $1
                      )
                    ORDER BY created_at DESC, id DESC
                    LIMIT $2
                    """,
                    user_id,
                    limit,
                    after,
                )
        return [_batch_row(row) for row in rows]

    async def list_results(
        self, batch_id: str, *, after: int | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Page through a batch's items in submission order, whatever their status.

        Args:
            batch_id: The batch.
            after: Return items with an id above this one (the last item id of
                the previous page); None starts at the first.
            limit: Page size.

        Returns:
            Result rows: ``id, custom_id, status, status_code, response, error,
            prompt_tokens, completion_tokens, cost_usd, request_id,
            completed_at``.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_RESULT_COLUMNS} FROM batch_items "
                "WHERE batch_id = $1 AND id > $2 ORDER BY id LIMIT $3",
                batch_id,
                after if after is not None else 0,
                limit,
            )
        return [_result_row(row) for row in rows]

    async def cancel_batch(
        self, batch_id: str, *, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """Cancel a validating or in-progress batch.

        The batch becomes ``cancelling`` and its queued items ``cancelled``;
        items already running finish, and the batch is ``cancelled`` as soon as
        none is left (at once when none was running). A batch in any other
        status is left as it is.

        Returns:
            The batch row as it now stands, or None when absent (or not
            *user_id*'s, when given).
        """
        async with self._pool.acquire() as conn, conn.transaction():
            status = await conn.fetchval(
                "SELECT status FROM batch_jobs "
                "WHERE id = $1 AND ($2::text IS NULL OR user_id = $2) FOR NO KEY UPDATE",
                batch_id,
                user_id,
            )
            if status is None:
                return None
            if status in ("validating", "in_progress"):
                await conn.execute(
                    "UPDATE batch_jobs SET status = 'cancelling', cancelling_at = now() "
                    "WHERE id = $1",
                    batch_id,
                )
                await self._settle(conn, batch_id, "cancelling")
            row = await conn.fetchrow(
                f"SELECT {_BATCH_COLUMNS} FROM batch_jobs WHERE id = $1", batch_id
            )
        return _batch_row(row)

    async def delete_batch(self, batch_id: str, *, user_id: str | None = None) -> bool:
        """Delete a batch with no live upstream call, including its items.

        Scoped to *user_id* when given. The batch's immutable model ids are
        read first, then their claim/heartbeat advisory locks are taken in
        sorted order before the batch is locked and rechecked. A heartbeat
        therefore waits rather than skipping an item lock and losing a renewal.

        Raises:
            BatchDeleteInProgress: At least one item has an unexpired running
                lease and may still be making an upstream call.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT model_ids FROM batch_jobs "
                "WHERE id = $1 AND ($2::text IS NULL OR user_id = $2)",
                batch_id,
                user_id,
            )
            if row is None:
                return False
            # model_ids is intentionally ordinary, queryable JSONB rather than
            # an opaque NUL envelope (model ids also live in a text column).
            model_ids = _load_plain_json(row["model_ids"])
            if not isinstance(model_ids, list) or not all(
                isinstance(model_id, str) for model_id in model_ids
            ):
                raise RuntimeError(f"Batch {batch_id!r} has invalid model_ids.")
            for model_id in sorted(set(model_ids)):
                await conn.execute(_CLAIM_LOCK_SQL, _CLAIM_LOCK_CLASS, model_id)
            found = await conn.fetchval(
                "SELECT TRUE FROM batch_jobs "
                "WHERE id = $1 AND ($2::text IS NULL OR user_id = $2) FOR UPDATE",
                batch_id,
                user_id,
            )
            if found is None:
                return False
            live = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM batch_items "
                "WHERE batch_id = $1 AND status = 'running' "
                "AND lease_expires_at > clock_timestamp())",
                batch_id,
            )
            if live:
                raise BatchDeleteInProgress(batch_id)
            result = await conn.execute("DELETE FROM batch_jobs WHERE id = $1", batch_id)
        return _rowcount(result) > 0

    async def delete_user_batches(self, user_id: str) -> int:
        """Delete every batch of a user, items included; return the batch count.

        Used by the admin hard-delete flow: the stored requests and results
        are the user's conversation data, owned by neither the operational
        nor the log store. Works before :meth:`initialize` has run (the purge
        reaches a store whose schema migration is still pending), and finds
        nothing to purge where the tables do not exist yet.
        """
        async with self._pool.acquire() as conn:
            try:
                result = await conn.execute(
                    """
                    DELETE FROM batch_jobs
                    WHERE id IN (
                        SELECT id FROM batch_jobs WHERE user_id = $1 ORDER BY id FOR UPDATE
                    )
                    """,
                    user_id,
                )
            except asyncpg.UndefinedTableError:
                return 0
        return _rowcount(result)

    # -- worker ------------------------------------------------------------------

    async def active_models(self) -> list[str]:
        """Return the models of every batch that items may be claimed from now, sorted."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT m.model_id
                FROM batch_jobs AS j
                CROSS JOIN LATERAL jsonb_array_elements_text(j.model_ids) AS m(model_id)
                WHERE j.status IN ('validating', 'in_progress')
                  AND (j.paused_until IS NULL OR j.paused_until <= now())
                  AND j.expires_at > now()
                ORDER BY m.model_id
                """
            )
        return [row["model_id"] for row in rows]

    async def claim_items(
        self,
        *,
        model_id: str,
        worker_id: str,
        limit: int,
        max_running: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        """Claim up to *limit* items of one model for *worker_id*.

        Holding the model's advisory lock, counts the model's running items
        whose lease is live across the deployment and claims at most
        ``max_running`` minus that many. Batches take turns: the one claimed
        from least recently (never first, then the oldest) gives its items in
        id order, then the next, until the room is filled. A queued item is
        claimable once its ``next_attempt_at`` has passed; a running one once
        its lease has run out (its worker died).

        A claimed item becomes ``running`` under a lease of *lease_seconds*,
        with its attempt counted; each batch it came from is marked claimed
        and moves from ``validating`` to ``in_progress``. The batches are
        locked and re-checked first, so a batch cancelled, failed, paused or
        deleted meanwhile gives nothing.

        Returns:
            The claimed items, in claim order: ``id, batch_id, custom_id,
            model_id, request, attempts`` (counting this one), ``error`` (the
            previous attempt's, or None), ``user_id`` and ``key_hash`` (the
            batch's).
        """
        if limit <= 0 or max_running <= 0:
            return []
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(_CLAIM_LOCK_SQL, _CLAIM_LOCK_CLASS, model_id)
            # A separate statement, so its snapshot includes every claim that
            # committed before this transaction got the lock.
            live = await conn.fetchval(
                "SELECT count(*) FROM batch_items "
                "WHERE model_id = $1 AND status = 'running' AND lease_expires_at > now()",
                model_id,
            )
            room = min(limit, max_running - live)
            if room <= 0:
                return []
            candidates = await conn.fetch(_CLAIM_CANDIDATES_SQL, model_id, room)
            if not candidates:
                return []
            owners = {
                row["id"]: row
                for row in await conn.fetch(
                    _CLAIM_LOCK_BATCHES_SQL, sorted({row["batch_id"] for row in candidates})
                )
            }
            if not owners:
                return []
            claimed = await conn.fetch(
                _CLAIM_ITEMS_SQL,
                [row["id"] for row in candidates],
                worker_id,
                float(lease_seconds),
                list(owners),
                model_id,
            )
            if not claimed:
                return []
            await conn.execute(
                _CLAIM_TOUCH_BATCHES_SQL, sorted({row["batch_id"] for row in claimed})
            )
        order = {row["id"]: position for position, row in enumerate(candidates)}
        return [
            {
                "id": row["id"],
                "batch_id": row["batch_id"],
                "custom_id": row["custom_id"],
                "model_id": row["model_id"],
                "request": _load_json(row["request"]),
                "attempts": row["attempts"],
                "error": _load_json(row["error"]),
                "user_id": owners[row["batch_id"]]["user_id"],
                "key_hash": owners[row["batch_id"]]["key_hash"],
            }
            for row in sorted(claimed, key=lambda row: order[row["id"]])
        ]

    async def heartbeat(self, *, worker_id: str, item_ids: list[int], lease_seconds: int) -> int:
        """Extend the leases of the items still running under *worker_id*'s claim.

        Claims and heartbeats share the per-model advisory lock, so a claimant
        cannot count an old committed lease as expired while its renewal is
        still uncommitted. Multi-model heartbeats take those locks in sorted
        order. An item another transaction has locked is skipped rather than
        waited for.

        Returns:
            The number of leases extended.
        """
        if not item_ids:
            return 0
        async with self._pool.acquire() as conn, conn.transaction():
            models = await conn.fetch(
                "SELECT DISTINCT model_id FROM batch_items "
                "WHERE id = ANY($1::bigint[]) ORDER BY model_id",
                list(item_ids),
            )
            for model_id in sorted({row["model_id"] for row in models}):
                await conn.execute(_CLAIM_LOCK_SQL, _CLAIM_LOCK_CLASS, model_id)
            result = await conn.execute(
                """
                UPDATE batch_items
                SET lease_expires_at = clock_timestamp() + make_interval(secs => $3)
                WHERE id IN (
                    SELECT id FROM batch_items
                    WHERE id = ANY($2::bigint[]) AND status = 'running' AND claimed_by = $1
                      AND lease_expires_at > clock_timestamp()
                    ORDER BY id
                    FOR NO KEY UPDATE SKIP LOCKED
                )
                """,
                worker_id,
                list(item_ids),
                float(lease_seconds),
            )
        return _rowcount(result)

    async def complete_item(
        self,
        *,
        item_id: int,
        worker_id: str,
        attempt: int,
        response: dict[str, Any] | None,
        error: dict[str, Any] | None,
        status_code: int | None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cost_usd: float | None = None,
        request_id: str | None = None,
    ) -> bool:
        """Record an item's final outcome: ``completed`` without *error*, else ``failed``.

        In the same transaction the batch counts the item as completed or
        failed, adds its cost, and settles.

        Returns:
            False, changing nothing, when the item is no longer running under
            this worker's claim at this attempt (its lease was lost, or the
            batch was deleted).
        """
        status = "completed" if error is None else "failed"
        # A NaN would make the batch's running total NaN for good.
        if cost_usd is not None and not math.isfinite(cost_usd):
            cost_usd = None
        async with self._pool.acquire() as conn, conn.transaction():
            batch = await self._guarded_item_write(
                conn,
                _COMPLETE_ITEM_SQL,
                item_id,
                worker_id,
                attempt,
                status,
                _dump_json(response),
                _dump_json(error),
                status_code,
                prompt_tokens,
                completion_tokens,
                cost_usd,
                request_id,
            )
            if batch is None:
                return False
            batch_id, batch_status = batch
            await conn.execute(
                """
                UPDATE batch_jobs
                SET completed_count = completed_count + $2,
                    failed_count = failed_count + $3,
                    cost_usd = cost_usd + $4
                WHERE id = $1
                """,
                batch_id,
                int(error is None),
                int(error is not None),
                float(cost_usd or 0.0),
            )
            await self._settle(conn, batch_id, batch_status)
        return True

    async def retry_item(
        self,
        *,
        item_id: int,
        worker_id: str,
        attempt: int,
        next_attempt_at: datetime,
        error: dict[str, Any],
    ) -> bool:
        """Queue an item again after a retryable failure, keeping *error* as its last.

        The attempt stays counted. The batch settles in the same transaction,
        so the item of a batch cancelled, failed or expired meanwhile takes
        the batch's verdict instead of running again.

        Returns:
            False, changing nothing, when the item is no longer running under
            this worker's claim at this attempt.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            batch = await self._guarded_item_write(
                conn,
                _RETRY_ITEM_SQL,
                item_id,
                worker_id,
                attempt,
                next_attempt_at,
                _dump_json(error),
            )
            if batch is None:
                return False
            await self._settle(conn, *batch)
        return True

    async def release_item(
        self,
        *,
        item_id: int,
        worker_id: str,
        attempt: int,
        next_attempt_at: datetime | None = None,
    ) -> bool:
        """Queue an item again without spending its attempt (the claim is handed back).

        Args:
            item_id: The item.
            worker_id: The claiming worker.
            attempt: The attempt the claim made.
            next_attempt_at: When it may be claimed again; None for at once.

        Returns:
            False, changing nothing, when the item is no longer running under
            this worker's claim at this attempt.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            batch = await self._guarded_item_write(
                conn, _RELEASE_ITEM_SQL, item_id, worker_id, attempt, next_attempt_at
            )
            if batch is None:
                return False
            await self._settle(conn, *batch)
        return True

    async def fail_batch(self, batch_id: str, error: dict[str, Any]) -> bool:
        """Fail a validating or in-progress batch (its owner's key is no longer valid).

        Its queued items fail as ``batch_failed`` and count as failed; items
        already running finish normally.

        Returns:
            True when the batch was failed, False when absent or in another
            status (a cancel in progress wins).
        """
        async with self._pool.acquire() as conn, conn.transaction():
            status = await conn.fetchval(
                "SELECT status FROM batch_jobs WHERE id = $1 FOR NO KEY UPDATE", batch_id
            )
            if status not in ("validating", "in_progress"):
                return False
            await conn.execute(
                "UPDATE batch_jobs SET status = 'failed', failed_at = now(), error = $2::jsonb "
                "WHERE id = $1",
                batch_id,
                _dump_json(error),
            )
            await self._settle(conn, batch_id, "failed")
        return True

    async def pause_user_batches(self, user_id: str, until: datetime) -> int:
        """Hold a user's validating and in-progress batches back from claims until *until*.

        Returns:
            The number of batches paused.
        """
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE batch_jobs SET paused_until = $2
                WHERE id IN (
                    SELECT id FROM batch_jobs
                    WHERE user_id = $1 AND status IN ('validating', 'in_progress')
                    ORDER BY id
                    FOR NO KEY UPDATE
                )
                """,
                user_id,
                until,
            )
        return _rowcount(result)

    async def sweep(self, *, retention_days: int) -> dict[str, int]:
        """Expire, finalize and purge; safe to run from several processes at once.

        1. A validating or in-progress batch past ``expires_at`` becomes
           ``expired``; its queued items expire as ``batch_expired`` and count
           as failed. Items already running finish normally.
        2. An active batch with no open item left is finalized (``completed``
           or ``cancelled``), and a batch no longer claimable from passes its
           verdict on to running items whose lease ran out.
        3. A finished batch whose final timestamp is more than
           *retention_days* old is deleted with its items (at most
           ``_PURGE_LIMIT`` per sweep).

        Returns:
            ``{"expired": n, "finalized": n, "purged": n}`` (batches).
        """
        counts = {"expired": 0, "finalized": 0, "purged": 0}
        async with self._pool.acquire() as conn:
            overdue = await conn.fetch(
                "SELECT id FROM batch_jobs WHERE status IN ('validating', 'in_progress') "
                "AND expires_at <= now() ORDER BY id"
            )
            for row in overdue:
                async with conn.transaction():
                    status = await conn.fetchval(
                        "SELECT status FROM batch_jobs WHERE id = $1 "
                        "AND status IN ('validating', 'in_progress') AND expires_at <= now() "
                        "FOR NO KEY UPDATE",
                        row["id"],
                    )
                    if status is None:
                        continue
                    await conn.execute(
                        "UPDATE batch_jobs SET status = 'expired', expired_at = now() "
                        "WHERE id = $1",
                        row["id"],
                    )
                    await self._settle(conn, row["id"], "expired")
                    counts["expired"] += 1

            for row in await conn.fetch(_SWEEP_SETTLE_CANDIDATES_SQL):
                async with conn.transaction():
                    status = await conn.fetchval(
                        "SELECT status FROM batch_jobs WHERE id = $1 FOR NO KEY UPDATE",
                        row["id"],
                    )
                    if status is not None and await self._settle(conn, row["id"], status):
                        counts["finalized"] += 1

            for _ in range(_PURGE_LIMIT):
                if await conn.fetchval(_PURGE_ONE_SQL, int(retention_days)) is None:
                    break
                counts["purged"] += 1
        return counts

    # -- admin: per-model caps ---------------------------------------------------

    async def list_model_limits(self) -> dict[str, int]:
        """Return every per-model cap override: model id to its maximum concurrency."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT model_id, max_concurrency FROM batch_model_limits")
        return {row["model_id"]: row["max_concurrency"] for row in rows}

    async def set_model_limit(
        self, model_id: str, max_concurrency: int, updated_by: str | None
    ) -> None:
        """Set (insert or replace) a model's cap; 0 pauses batch work on it.

        Raises:
            ValueError: *max_concurrency* is negative.
        """
        if max_concurrency < 0:
            raise ValueError("max_concurrency must be >= 0")
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO batch_model_limits (model_id, max_concurrency, updated_by, updated_at)
                VALUES ($1, $2, $3, now())
                ON CONFLICT (model_id) DO UPDATE SET
                    max_concurrency = EXCLUDED.max_concurrency,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = EXCLUDED.updated_at
                """,
                model_id,
                int(max_concurrency),
                updated_by,
            )

    async def delete_model_limit(self, model_id: str) -> bool:
        """Remove a model's override, so the default cap applies. True when one existed."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM batch_model_limits WHERE model_id = $1", model_id
            )
        return _rowcount(result) > 0

    async def model_stats(self) -> dict[str, dict[str, int]]:
        """Count open items per model.

        Returns:
            ``{model_id: {"running": n, "queued": n}}`` for every model with an
            open item; ``running`` counts live leases only.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT model_id,
                       count(*) FILTER (
                           WHERE status = 'running' AND lease_expires_at > now()
                       ) AS running,
                       count(*) FILTER (WHERE status = 'queued') AS queued
                FROM batch_items
                WHERE status IN ('queued', 'running')
                GROUP BY model_id
                """
            )
        return {
            row["model_id"]: {"running": row["running"], "queued": row["queued"]} for row in rows
        }

    # -- internals ---------------------------------------------------------------

    async def _guarded_item_write(
        self,
        conn: Any,
        sql: str,
        item_id: int,
        worker_id: str,
        attempt: int,
        *args: Any,
    ) -> tuple[str, str] | None:
        """Lock an item's batch, then run a worker write carrying ``_ITEM_GUARD``.

        Returns:
            ``(batch_id, batch status)`` when the write applied, None when the
            item or its batch is gone or the guard did not match.
        """
        batch = await conn.fetchrow(_LOCK_ITEM_BATCH_SQL, item_id)
        if batch is None:
            return None
        result = await conn.execute(sql, item_id, worker_id, attempt, *args)
        if _rowcount(result) == 0:
            return None
        return batch["id"], batch["status"]

    async def _settle(self, conn: Any, batch_id: str, status: str) -> str | None:
        """Bring a batch's open items in line with its *status*; finalize it when none is left.

        Must run inside the caller's transaction, holding the batch row's lock.

        Returns:
            The status the batch moved to (``completed`` or ``cancelled``), or
            None when it did not move.
        """
        verdict = _ITEM_VERDICTS.get(status)
        if verdict is not None:
            item_status, error = verdict
            settled = _rowcount(
                await conn.execute(_SETTLE_ITEMS_SQL, batch_id, item_status, _dump_json(error))
            )
            if settled and status != "cancelling":
                await conn.execute(
                    "UPDATE batch_jobs SET failed_count = failed_count + $2 WHERE id = $1",
                    batch_id,
                    settled,
                )
        final = _FINALIZE_SQL.get(status)
        if final is None or await conn.fetchval(_HAS_OPEN_ITEMS_SQL, batch_id):
            return None
        final_status, sql = final
        await conn.execute(sql, batch_id)
        return final_status


async def _missing_schema_objects(conn: Any) -> set[str]:
    """Names of the batch tables and indexes the database does not have yet."""
    names = [name for name, _ in (*_TABLES, *_INDEXES)]
    rows = await conn.fetch(
        "SELECT name FROM unnest($1::text[]) AS name WHERE to_regclass(name) IS NULL", names
    )
    return {row["name"] for row in rows}


def _batch_row(row: Any) -> dict[str, Any]:
    """A ``batch_jobs`` row as the dict callers get, JSON decoded and cost a float."""
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "key_hash": row["key_hash"],
        "endpoint": row["endpoint"],
        "status": row["status"],
        "model_ids": list(_load_plain_json(row["model_ids"]) or []),
        "request_count": row["request_count"],
        "completed_count": row["completed_count"],
        "failed_count": row["failed_count"],
        "metadata": _load_json(row["metadata"]),
        "error": _load_json(row["error"]),
        "cost_usd": float(row["cost_usd"] or 0),
        "created_at": row["created_at"],
        "in_progress_at": row["in_progress_at"],
        "completed_at": row["completed_at"],
        "failed_at": row["failed_at"],
        "expired_at": row["expired_at"],
        "cancelling_at": row["cancelling_at"],
        "cancelled_at": row["cancelled_at"],
        "expires_at": row["expires_at"],
        "paused_until": row["paused_until"],
    }


def _result_row(row: Any) -> dict[str, Any]:
    """A ``batch_items`` row as a result, JSON decoded and cost a float."""
    return {
        "id": row["id"],
        "custom_id": row["custom_id"],
        "status": row["status"],
        "status_code": row["status_code"],
        "response": _load_json(row["response"]),
        "error": _load_json(row["error"]),
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "cost_usd": float(row["cost_usd"]) if row["cost_usd"] is not None else None,
        "request_id": row["request_id"],
        "completed_at": row["completed_at"],
    }


def _rowcount(result: Any) -> int:
    """The row count of an asyncpg command tag such as ``UPDATE 3``."""
    try:
        return int(result.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


def _load_plain_json(value: Any) -> Any:
    """Decode JSON text without treating a reserved payload envelope specially."""
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Failed to decode a batch JSON column", exc_info=True)
        return None


def _dump_json(value: Any) -> str | None:
    """Encode a value for a JSONB column; None stays SQL NULL.

    Ordinary values retain their JSONB shape. A value containing a NUL uses a
    versioned JSON-array envelope because PostgreSQL JSONB cannot represent
    U+0000, even as an escape.
    """
    if value is None:
        return None
    encoded = json.dumps(value)
    if _contains_nul(value) or _is_json_envelope(value):
        payload = base64.b64encode(encoded.encode("utf-8")).decode("ascii")
        return json.dumps([_JSON_ENVELOPE_MARKER, payload])
    return encoded


def _load_json(value: Any) -> Any:
    """Decode ordinary JSONB and the lossless NUL envelope.

    Depending on its codec, asyncpg may return either JSON text or an
    already-decoded object.
    """
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            # The superseded string-envelope prefix was never a valid stored
            # payload shape, but preserve a direct legacy collision.
            if value.startswith(_JSON_ENVELOPE_MARKER):
                return value
            # Invalid JSON in a JSONB column signals data corruption (manual
            # edit / migration bug); surface it rather than silently dropping.
            logger.warning("Failed to decode a batch JSON column", exc_info=True)
            return None
    else:
        decoded = value
    if _is_json_envelope(decoded):
        return _load_json_envelope(decoded)
    return decoded


def _contains_nul(value: Any) -> bool:
    """Return whether a JSON-compatible value contains U+0000 in a key or value."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_contains_nul(key) or _contains_nul(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_nul(item) for item in value)
    return False


def _is_json_envelope(value: Any) -> bool:
    """Return whether *value* has the exact reserved envelope shape."""
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and value[0] == _JSON_ENVELOPE_MARKER
        and isinstance(value[1], str)
    )


def _load_json_envelope(value: list[Any] | tuple[Any, ...]) -> Any:
    """Decode one JSON-array envelope, preserving malformed collisions."""
    try:
        raw = base64.b64decode(value[1], validate=True).decode("utf-8")
        return json.loads(raw)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        # This is not an envelope produced by _dump_json. Treat it as an
        # ordinary array so a reserved-shape collision remains lossless.
        return value
