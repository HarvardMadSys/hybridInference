"""Persistence for batch inference (``/v1/batches``).

A batch is one row of ``batch_jobs`` plus one row of ``batch_items`` per
request; ``batch_model_limits`` holds the per-model concurrency caps an
administrator sets. Like :mod:`serving.storage.responses_store` this is a small,
self-contained store over the shared Postgres pool, outside the
``OperationalStore`` contract. Files (``batch_files``) live in
:mod:`serving.storage.batch_file_store` and webhooks in
:mod:`serving.storage.batch_webhook_store`; their tables are created here, with
the rest of the batch schema, so one migration orders every foreign key.

Every gateway process runs a worker (:mod:`serving.batch.scheduler`) that claims
items from here, so whatever coordinates the processes lives in SQL:

- **Kinds and phases.** A batch has an ``api_surface`` -- how its requests
  arrived: ``openai_inline`` (a ``requests`` array) or ``openai_file`` (an
  uploaded JSONL file); ``anthropic`` only on batches the retired Message
  Batches API stored -- a ``target`` (``chat``, ``embeddings``, ``messages``,
  ``responses``) and a ``schema_version``. A worker only touches
  the kinds it knows (:data:`SUPPORTED_JOB_KINDS`): the claim, the list of
  models with work and the sweep all share one predicate
  (``_CLAIMABLE_JOB``), so a batch a newer worker created is never run, settled
  or expired by an older one. Independently of its execution ``status`` a batch
  has a ``phase``: ``validating`` (an uploaded file is still being checked; its
  staged items are not claimable), ``runnable``, ``sealing`` (every item is
  final and its output files are being fixed) and ``sealed`` (its results are
  published and never change again).
- **Claims** for one model are serialized across processes by a
  transaction-scoped advisory lock keyed on the model, and count the model's
  live leases while holding it, so a model's cap holds deployment-wide. Users
  take turns first and batches second: the user claimed from least recently
  goes first, and splitting work into more batches does not earn a user more
  turns. A user's own running items are capped by the worker, per role
  (``serving.batch.scheduler``), not here.
- **Leases**: a claimed item stays ``running`` until ``lease_expires_at``, which
  its worker keeps extending (:meth:`BatchStore.heartbeat`). The item of a
  worker that died is claimable again once its lease has run out, and that
  attempt counts.
- **Attempts**: every claim opens a ``batch_attempts`` row keyed by the item's
  ``attempt_seq``, which is never decremented and is the fencing token every
  worker write carries. An attempt records whether its upstream call began,
  what it was billed (observed at the handler's own billing point, never
  billed a second time here) and how it ended. An attempt whose worker
  vanished after dispatch has an **unknown** cost: it is counted, never taken
  for zero.
- **Guards**: a worker's write (complete, retry, release) applies only while
  the item is still running under that worker's claim and attempt, so a lost
  lease or a deleted batch makes it a no-op (``False``) instead of overwriting
  newer state. Recording an attempt twice changes nothing the second time.
- **Settling** (``_settle``) runs in the transaction of every item and batch
  transition: a ``cancelling``, ``failed`` or ``expired`` batch passes its
  verdict on to its queued items (and to running items whose lease ran out,
  which no worker can claim back), and a batch with no open item left is
  finalized. Inline (and legacy Anthropic) batches are sealed in that same transaction;
  a file batch freezes its final status in ``pending_terminal_status`` and
  enters ``sealing``, where a worker renders its output files and publishes
  them together with the final status. Neither expiry nor a cancel changes a
  batch once it is sealing. A late completion or retry never undoes a cancel.
- **Admission** for one user is serialized by a per-user advisory lock: the
  idempotency decision, the active-batch and queued-request caps and the
  insert happen in one transaction, so concurrent creates cannot pass a cap
  together.
- **Lock order**: claims, heartbeats and user deletes take model advisory locks
  in sorted order before row locks; a claim then takes per-user claim locks in
  sorted order. Admission takes the user admission lock before any row lock.
  Worker writes lock a batch before its item, and several batch rows in id
  order. No transaction reverses a shared lock order, so two transactions
  cannot wait on each other in a cycle.

Schema creation follows :mod:`serving.storage.log_schema`: the catalog is read
first, so a settled database takes no DDL lock at all, and whatever is missing
is created in one transaction under the bounded lock wait. An advisory lock
serializes the gateway processes that boot at once, since two concurrent
``CREATE TABLE IF NOT EXISTS`` can still collide on the catalog's unique index.
Columns added to the original two tables carry constant defaults, which
PostgreSQL records in the catalog without rewriting or scanning the table.
"""

from __future__ import annotations

import base64
import json
import math
import secrets
from typing import TYPE_CHECKING, Any

import asyncpg

from serving.storage.log_schema import bounded_ddl, execute_ddl, existing_columns
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


class BatchInputUnavailable(RuntimeError):
    """A file batch's input upload is no longer available to create it from."""

    def __init__(self, file_id: str) -> None:
        self.file_id = file_id
        super().__init__(f"File {file_id!r} is not available.")


class BatchLimitExceeded(RuntimeError):
    """Admission refused: a per-user cap would be exceeded.

    ``kind`` is ``active_batches`` or ``queued_requests``; ``limit`` the cap and
    ``current`` what the user already holds.
    """

    def __init__(self, kind: str, *, limit: int, current: int, requested: int = 0) -> None:
        self.kind = kind
        self.limit = limit
        self.current = current
        self.requested = requested
        super().__init__(f"{kind}: {current} + {requested} exceeds {limit}")


class IdempotencyConflict(RuntimeError):
    """An ``Idempotency-Key`` was reused with a different request."""

    def __init__(self, batch_id: str) -> None:
        self.batch_id = batch_id
        super().__init__("Idempotency-Key reused with a different request")


class IdempotencyTombstoned(RuntimeError):
    """The batch an ``Idempotency-Key`` created has since been deleted."""

    def __init__(self, batch_id: str) -> None:
        self.batch_id = batch_id
        super().__init__("The batch this Idempotency-Key created was deleted")


# -- batch kinds ----------------------------------------------------------------

#: How a batch's requests arrived. The values keep the names they were first
#: stored under; they are internal and never shown as separate APIs.
SURFACE_OPENAI_INLINE = "openai_inline"
SURFACE_OPENAI_FILE = "openai_file"
#: Batches the retired Message Batches API (``/v1/messages/batches``) stored.
#: Nothing creates one any more; they are still run, read and purged.
SURFACE_ANTHROPIC = "anthropic"

TARGET_CHAT = "chat"
TARGET_EMBEDDINGS = "embeddings"
TARGET_MESSAGES = "messages"
TARGET_RESPONSES = "responses"

#: Version 1: batches stored before surfaces and phases existed (every one an
#: inline chat batch). Version 2: everything this code stores.
SCHEMA_VERSION = 2

#: The (surface, target) pairs this worker can run, at :data:`SCHEMA_VERSION`
#: or below. Claims, the list of models with work and every sweep step are
#: restricted to them. Messages through ``/v1/batches`` (inline or from a file)
#: are pairs a worker of an earlier version does not list, so during a rolling
#: upgrade only this version runs, validates, seals or expires them.
SUPPORTED_JOB_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        (SURFACE_OPENAI_INLINE, TARGET_CHAT),
        (SURFACE_OPENAI_INLINE, TARGET_EMBEDDINGS),
        (SURFACE_OPENAI_INLINE, TARGET_RESPONSES),
        (SURFACE_OPENAI_INLINE, TARGET_MESSAGES),
        (SURFACE_OPENAI_FILE, TARGET_CHAT),
        (SURFACE_OPENAI_FILE, TARGET_EMBEDDINGS),
        (SURFACE_OPENAI_FILE, TARGET_RESPONSES),
        (SURFACE_OPENAI_FILE, TARGET_MESSAGES),
        (SURFACE_ANTHROPIC, TARGET_MESSAGES),
    }
)

PHASE_VALIDATING = "validating"
PHASE_RUNNABLE = "runnable"
PHASE_SEALING = "sealing"
PHASE_SEALED = "sealed"

#: A batch in one of these statuses is finished: its status never changes again
#: (items already running when it expired or failed may still finish).
TERMINAL_BATCH_STATUSES: frozenset[str] = frozenset({"completed", "failed", "expired", "cancelled"})
#: Batches in these statuses count toward a user's cap on active batches.
ACTIVE_BATCH_STATUSES: frozenset[str] = frozenset({"validating", "in_progress", "cancelling"})

#: How long an idempotency record outlives the batch's end (or its deletion).
IDEMPOTENCY_RETENTION_SECONDS = 24 * 3600

# Two-key advisory locks. Each class plus ``hashtext(<key>)``: claims for one
# model, a user's admission. Two keys sharing a hash merely
# serialize. The two-key space is separate from the one-key space the other
# locks in this layer use.
_CLAIM_LOCK_CLASS = 0x42415443  # ASCII "BATC"
_USER_ADMISSION_LOCK_CLASS = 0x42415541  # "BAUA"
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


def _supported_kinds_sql(
    alias: str, kinds: frozenset[tuple[str, str]] = SUPPORTED_JOB_KINDS
) -> str:
    """SQL matching the batches this worker supports (``alias`` is the jobs alias)."""
    pairs = ", ".join(f"('{surface}', '{target}')" for surface, target in sorted(kinds))
    return f"({alias}.schema_version <= {SCHEMA_VERSION} AND ({alias}.api_surface, {alias}.target) IN ({pairs}))"


def display_status_sql(alias: str) -> str:
    """A batch's status as the API and the console show it, as SQL (``alias``: the jobs alias).

    The twin of ``serving.batch.renderers.display_status``, so filtering on a
    status matches exactly the batches listed with it: a file batch is
    ``validating`` until its input is checked, ``finalizing`` (or
    ``cancelling``) while its result files are sealed, and shows its final
    status only once they are published; any other batch shows its status.
    """
    return f"""(CASE
        WHEN {alias}.api_surface <> '{SURFACE_OPENAI_FILE}' THEN {alias}.status
        WHEN {alias}.phase = '{PHASE_VALIDATING}' THEN
            CASE WHEN {alias}.status = 'cancelling' THEN 'cancelling' ELSE 'validating' END
        WHEN {alias}.phase = '{PHASE_SEALING}' THEN
            CASE WHEN {alias}.pending_terminal_status = 'cancelled'
                 THEN 'cancelling' ELSE 'finalizing' END
        WHEN {alias}.phase = '{PHASE_SEALED}' THEN {alias}.status
        WHEN {alias}.status IN ('expired', 'failed') THEN 'in_progress'
        ELSE {alias}.status
    END)"""


def _claimable_job_sql(alias: str) -> str:
    """The one predicate for a batch whose items may be claimed now."""
    return (
        f"({alias}.phase = 'runnable' "
        f"AND {alias}.status IN ('validating', 'in_progress') "
        f"AND ({alias}.paused_until IS NULL OR {alias}.paused_until <= now()) "
        f"AND {alias}.expires_at > now() "
        f"AND {alias}.delete_requested_at IS NULL "
        f"AND {_supported_kinds_sql(alias)})"
    )


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
    (
        # One row per attempt at an item: what it was billed and how it ended.
        "batch_attempts",
        """
        CREATE TABLE IF NOT EXISTS batch_attempts (
            item_id BIGINT NOT NULL REFERENCES batch_items(id) ON DELETE CASCADE,
            attempt_no INTEGER NOT NULL,
            batch_id TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            dispatched_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            outcome TEXT,
            status_code INTEGER,
            request_id TEXT,
            usage JSONB,
            cost_usd NUMERIC(14, 8),
            cost_status TEXT,
            error JSONB,
            PRIMARY KEY (item_id, attempt_no)
        )
        """,
    ),
    (
        # Idempotency-Key records. Deliberately not a foreign key of
        # batch_jobs: a deleted batch leaves its record as a tombstone.
        "batch_idempotency",
        """
        CREATE TABLE IF NOT EXISTS batch_idempotency (
            owner_id TEXT NOT NULL,
            api_surface TEXT NOT NULL,
            idem_key TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ,
            PRIMARY KEY (owner_id, api_surface, idem_key)
        )
        """,
    ),
    (
        # Uploaded input files and the virtual output/error files of sealed
        # batches. An upload belongs to its user, not to any batch.
        "batch_files",
        """
        CREATE TABLE IF NOT EXISTS batch_files (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            purpose TEXT NOT NULL,
            kind TEXT NOT NULL,
            filename TEXT NOT NULL,
            status TEXT NOT NULL,
            bytes BIGINT NOT NULL DEFAULT 0,
            sha256 TEXT,
            line_count INTEGER,
            batch_id TEXT REFERENCES batch_jobs(id) ON DELETE CASCADE,
            generation INTEGER,
            renderer_version INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ready_at TIMESTAMPTZ,
            deleted_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ,
            read_lease_until TIMESTAMPTZ
        )
        """,
    ),
    (
        "batch_file_chunks",
        """
        CREATE TABLE IF NOT EXISTS batch_file_chunks (
            file_id TEXT NOT NULL REFERENCES batch_files(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            data BYTEA NOT NULL,
            PRIMARY KEY (file_id, seq)
        )
        """,
    ),
    (
        # A download in progress: physical deletion of what it reads waits for
        # its lease to lapse. No foreign keys, on purpose.
        "batch_read_leases",
        """
        CREATE TABLE IF NOT EXISTS batch_read_leases (
            lease_id TEXT PRIMARY KEY,
            batch_id TEXT,
            file_id TEXT,
            expires_at TIMESTAMPTZ NOT NULL
        )
        """,
    ),
    (
        # Per-user scheduling state: whose turn it is, and today's batch spend.
        "batch_user_state",
        """
        CREATE TABLE IF NOT EXISTS batch_user_state (
            user_id TEXT PRIMARY KEY,
            last_claimed_at TIMESTAMPTZ,
            claim_seq BIGINT
        )
        """,
    ),
    (
        # Service order for fair claiming: a user (or batch) served later in
        # one claim gets a later number, and the lowest number goes first.
        "batch_claim_seq",
        "CREATE SEQUENCE IF NOT EXISTS batch_claim_seq",
    ),
    (
        # Interactive requests in flight per process and model: the shared view
        # every worker yields to.
        "batch_interactive_load",
        """
        CREATE TABLE IF NOT EXISTS batch_interactive_load (
            process_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            in_flight INTEGER NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (process_id, model_id)
        )
        """,
    ),
    (
        "batch_webhooks",
        """
        CREATE TABLE IF NOT EXISTS batch_webhooks (
            user_id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            secret_encrypted TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
    ),
    (
        # The webhook outbox, written in the transaction that seals a batch.
        "batch_webhook_events",
        """
        CREATE TABLE IF NOT EXISTS batch_webhook_events (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload JSONB NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_error TEXT,
            last_status_code INTEGER,
            lease_owner TEXT,
            lease_expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            delivered_at TIMESTAMPTZ
        )
        """,
    ),
)

# Columns this version adds to the two original tables. Every default is a
# constant, so adding one is a catalog-only change.
_COLUMN_MIGRATIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "batch_jobs",
        (
            ("api_surface", "TEXT NOT NULL DEFAULT 'openai_inline'"),
            ("target", "TEXT NOT NULL DEFAULT 'chat'"),
            ("schema_version", "INTEGER NOT NULL DEFAULT 1"),
            ("protocol_profile", "TEXT"),
            ("phase", "TEXT NOT NULL DEFAULT 'runnable'"),
            ("pending_terminal_status", "TEXT"),
            ("validation_error", "JSONB"),
            ("input_file_id", "TEXT"),
            ("output_file_id", "TEXT"),
            ("error_file_id", "TEXT"),
            ("retention_seconds", "BIGINT"),
            ("results_expires_at", "TIMESTAMPTZ"),
            ("seal_generation", "INTEGER NOT NULL DEFAULT 0"),
            ("work_owner", "TEXT"),
            ("work_expires_at", "TIMESTAMPTZ"),
            ("finalizing_at", "TIMESTAMPTZ"),
            ("sealed_at", "TIMESTAMPTZ"),
            ("cancelled_count", "INTEGER NOT NULL DEFAULT 0"),
            ("expired_count", "INTEGER NOT NULL DEFAULT 0"),
            ("prompt_tokens", "BIGINT NOT NULL DEFAULT 0"),
            ("completion_tokens", "BIGINT NOT NULL DEFAULT 0"),
            ("unknown_cost_count", "INTEGER NOT NULL DEFAULT 0"),
            ("delete_requested_at", "TIMESTAMPTZ"),
            ("idempotency_key", "TEXT"),
            ("claim_seq", "BIGINT"),
            ("read_lease_until", "TIMESTAMPTZ"),
        ),
    ),
    (
        "batch_items",
        (
            ("attempt_seq", "INTEGER NOT NULL DEFAULT 0"),
            ("usage", "JSONB"),
            ("cost_status", "TEXT"),
            ("line_no", "INTEGER"),
        ),
    ),
)

# ``(name, ddl)`` per index, created after the tables. None of these indexes a
# table that predates this version except ``batch_jobs`` (one row per batch,
# small), so building them inside the migration transaction is brief.
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
        # Batches a worker must validate or seal.
        "idx_batch_jobs_work",
        "CREATE INDEX IF NOT EXISTS idx_batch_jobs_work ON batch_jobs (work_expires_at) "
        "WHERE phase IN ('validating', 'sealing')",
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
    (
        "idx_batch_attempts_batch",
        "CREATE INDEX IF NOT EXISTS idx_batch_attempts_batch ON batch_attempts (batch_id)",
    ),
    (
        "idx_batch_idempotency_batch",
        "CREATE INDEX IF NOT EXISTS idx_batch_idempotency_batch ON batch_idempotency (batch_id)",
    ),
    (
        "idx_batch_idempotency_expiry",
        "CREATE INDEX IF NOT EXISTS idx_batch_idempotency_expiry ON batch_idempotency (expires_at) "
        "WHERE expires_at IS NOT NULL",
    ),
    (
        "idx_batch_files_user",
        "CREATE INDEX IF NOT EXISTS idx_batch_files_user "
        "ON batch_files (user_id, created_at DESC, id DESC)",
    ),
    (
        "idx_batch_files_batch",
        "CREATE INDEX IF NOT EXISTS idx_batch_files_batch ON batch_files (batch_id)",
    ),
    (
        "idx_batch_read_leases_batch",
        "CREATE INDEX IF NOT EXISTS idx_batch_read_leases_batch ON batch_read_leases (batch_id)",
    ),
    (
        "idx_batch_read_leases_file",
        "CREATE INDEX IF NOT EXISTS idx_batch_read_leases_file ON batch_read_leases (file_id)",
    ),
    (
        "idx_batch_webhook_events_due",
        "CREATE INDEX IF NOT EXISTS idx_batch_webhook_events_due "
        "ON batch_webhook_events (next_attempt_at) WHERE status = 'pending'",
    ),
    (
        "idx_batch_webhook_events_user",
        "CREATE INDEX IF NOT EXISTS idx_batch_webhook_events_user "
        "ON batch_webhook_events (user_id, created_at DESC)",
    ),
)

_BATCH_COLUMNS = (
    "id, user_id, key_hash, endpoint, status, model_ids, request_count, completed_count, "
    "failed_count, metadata, error, cost_usd, created_at, in_progress_at, completed_at, "
    "failed_at, expired_at, cancelling_at, cancelled_at, expires_at, paused_until, "
    "api_surface, target, schema_version, protocol_profile, phase, pending_terminal_status, "
    "validation_error, input_file_id, output_file_id, error_file_id, results_expires_at, "
    "finalizing_at, sealed_at, cancelled_count, expired_count, prompt_tokens, "
    "completion_tokens, unknown_cost_count, idempotency_key"
)
_RESULT_COLUMNS = (
    "id, custom_id, status, status_code, response, error, prompt_tokens, completion_tokens, "
    "cost_usd, request_id, completed_at, usage, cost_status, attempts, model_id"
)
# Filled by COPY in this order; every other column takes its default, and
# ``id`` follows the order the rows arrive in.
_ITEM_COPY_COLUMNS = ("batch_id", "custom_id", "model_id", "request", "line_no")
_FAILED_ITEM_COPY_COLUMNS = (
    "batch_id",
    "custom_id",
    "model_id",
    "request",
    "line_no",
    "status",
    "error",
    "status_code",
    "completed_at",
)

# Candidates for one claim: the claimable batches that hold the model, each
# contributing its claimable items in id order. Users take turns first (the
# user served least recently first, by ``claim_seq``), then their batches
# (interleaved, the one served least recently first), so splitting the same
# work into more batches earns no extra turns. Users already at their running cap ($3 and $4: user
# ids and remaining room, $5 the default room; 0 for no cap) are skipped. Read
# without locks; the claim re-checks.
_CLAIM_CANDIDATES_SQL = f"""
    WITH jobs AS (
        SELECT j.id, j.user_id, j.claim_seq, j.created_at, s.claim_seq AS user_seq
        FROM batch_jobs AS j
        LEFT JOIN batch_user_state AS s ON s.user_id = j.user_id
        WHERE {_claimable_job_sql("j")}
          AND j.model_ids ? $1
    ),
    candidates AS (
        SELECT jobs.user_id, jobs.user_seq, jobs.id AS batch_id, i.id,
               jobs.claim_seq, jobs.created_at,
               row_number() OVER (PARTITION BY jobs.id ORDER BY i.id) AS batch_pos
        FROM jobs
        CROSS JOIN LATERAL (
            SELECT bi.id
            FROM batch_items AS bi
            WHERE bi.batch_id = jobs.id
              AND bi.model_id = $1
              AND bi.status IN ('queued', 'running')
              AND (
                  (bi.status = 'queued'
                   AND (bi.next_attempt_at IS NULL OR bi.next_attempt_at <= now()))
                  OR (bi.status = 'running' AND bi.lease_expires_at <= now())
              )
            ORDER BY bi.id
            LIMIT $2
        ) AS i
    ),
    ranked AS (
        SELECT c.*,
               row_number() OVER (
                   PARTITION BY c.user_id
                   ORDER BY c.batch_pos, c.claim_seq ASC NULLS FIRST, c.created_at, c.batch_id
               ) AS user_pos
        FROM candidates AS c
    )
    SELECT r.id, r.batch_id, r.user_id
    FROM ranked AS r
    ORDER BY r.user_pos, r.user_seq ASC NULLS FIRST, r.user_id, r.batch_pos, r.id
    LIMIT $2
"""

# Locks the candidates' batches in id order, keeping those still claimable.
_CLAIM_LOCK_BATCHES_SQL = f"""
    SELECT j.id, j.user_id, j.key_hash, j.api_surface, j.target, j.protocol_profile, j.endpoint
    FROM batch_jobs AS j
    WHERE j.id = ANY($1::text[])
      AND {_claimable_job_sql("j")}
    ORDER BY j.id
    FOR NO KEY UPDATE
"""

# Items about to be claimed back from a worker whose lease ran out: their open
# attempt is abandoned.
_CLAIM_EXPIRED_SQL = """
    SELECT id, attempt_seq
    FROM batch_items
    WHERE id = ANY($1::bigint[]) AND status = 'running' AND lease_expires_at <= now()
    ORDER BY id
    FOR NO KEY UPDATE
"""

_CLAIM_ITEMS_SQL = """
    UPDATE batch_items
    SET status = 'running',
        attempts = attempts + 1,
        attempt_seq = attempt_seq + 1,
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
    RETURNING id, batch_id, custom_id, model_id, request, attempts, attempt_seq, error
"""

_OPEN_ATTEMPTS_SQL = """
    INSERT INTO batch_attempts (item_id, attempt_no, batch_id, worker_id)
    SELECT * FROM unnest($1::bigint[], $2::int[], $3::text[], $4::text[])
    ON CONFLICT (item_id, attempt_no) DO NOTHING
"""

# Marks the batches a claim served, each with its place in the service order.
_CLAIM_TOUCH_BATCHES_SQL = """
    UPDATE batch_jobs AS j
    SET last_claimed_at = now(),
        claim_seq = t.seq,
        in_progress_at = CASE WHEN j.status = 'validating' THEN now() ELSE j.in_progress_at END,
        status = CASE WHEN j.status = 'validating' THEN 'in_progress' ELSE j.status END
    FROM unnest($1::text[], $2::bigint[]) AS t(id, seq)
    WHERE j.id = t.id
"""

# Marks the users a claim served, each with its place in the service order.
_CLAIM_TOUCH_USERS_SQL = """
    INSERT INTO batch_user_state (user_id, last_claimed_at, claim_seq)
    SELECT t.user_id, now(), t.seq FROM unnest($1::text[], $2::bigint[]) AS t(user_id, seq)
    ON CONFLICT (user_id) DO UPDATE SET
        last_claimed_at = EXCLUDED.last_claimed_at, claim_seq = EXCLUDED.claim_seq
"""

_NEXT_SEQ_SQL = "SELECT nextval('batch_claim_seq') AS seq FROM generate_series(1, $1)"

# The batch of an item, locked before the item itself (see the lock order).
_LOCK_ITEM_BATCH_SQL = """
    SELECT id, status, user_id
    FROM batch_jobs
    WHERE id = (SELECT batch_id FROM batch_items WHERE id = $1)
    FOR NO KEY UPDATE
"""

# The guard every worker write carries: the item is still running under this
# worker's claim, at this attempt.
_ITEM_GUARD = "id = $1 AND status = 'running' AND claimed_by = $2 AND attempt_seq = $3"

_COMPLETE_ITEM_SQL = f"""
    UPDATE batch_items
    SET status = $4, response = $5::jsonb, error = $6::jsonb, status_code = $7,
        prompt_tokens = $8, completion_tokens = $9,
        cost_usd = CASE WHEN $10::numeric IS NULL THEN cost_usd
                        ELSE COALESCE(cost_usd, 0) + $10::numeric END,
        request_id = $11, usage = $12::jsonb,
        cost_status = CASE WHEN $13::text = 'unknown' OR cost_status = 'unknown' THEN 'unknown'
                           ELSE 'known' END,
        completed_at = now(), lease_expires_at = NULL, next_attempt_at = NULL
    WHERE {_ITEM_GUARD}
"""

_RETRY_ITEM_SQL = f"""
    UPDATE batch_items
    SET status = 'queued', next_attempt_at = $4, error = $5::jsonb,
        cost_usd = CASE WHEN $6::numeric IS NULL THEN cost_usd
                        ELSE COALESCE(cost_usd, 0) + $6::numeric END,
        cost_status = CASE WHEN $7::text = 'unknown' OR cost_status = 'unknown' THEN 'unknown'
                           ELSE cost_status END,
        claimed_by = NULL, lease_expires_at = NULL
    WHERE {_ITEM_GUARD}
"""

_RELEASE_ITEM_SQL = f"""
    UPDATE batch_items
    SET status = 'queued', attempts = GREATEST(attempts - 1, 0), next_attempt_at = $4,
        cost_status = CASE WHEN $5::text = 'unknown' THEN 'unknown' ELSE cost_status END,
        claimed_by = NULL, lease_expires_at = NULL
    WHERE {_ITEM_GUARD}
"""

# Closes an attempt once; a second report changes nothing.
_CLOSE_ATTEMPT_SQL = """
    UPDATE batch_attempts
    SET finished_at = now(), outcome = $3, status_code = $4, request_id = $5,
        usage = $6::jsonb, cost_usd = $7, cost_status = $8, error = $9::jsonb
    WHERE item_id = $1 AND attempt_no = $2 AND outcome IS NULL
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

# Running items whose lease ran out, in a batch that can no longer be claimed
# from: their attempt was abandoned by a worker that is gone.
_EXPIRED_RUNNING_SQL = """
    SELECT id, attempt_seq FROM batch_items
    WHERE batch_id = $1 AND status = 'running' AND lease_expires_at <= now()
    ORDER BY id
"""

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

# The final status of a batch once no open item is left, by its current status.
_FINAL_STATUS: dict[str, str] = {
    "validating": "completed",
    "in_progress": "completed",
    "cancelling": "cancelled",
    "failed": "failed",
    "expired": "expired",
}
# The timestamp column a final status sets.
_STATUS_TIMESTAMP: dict[str, str] = {
    "completed": "completed_at",
    "cancelled": "cancelled_at",
    "failed": "failed_at",
    "expired": "expired_at",
}

# Batches a sweep settles: active ones with no open item left, and those that
# can no longer be claimed from but hold a running item whose lease ran out.
_SWEEP_SETTLE_CANDIDATES_SQL = f"""
    SELECT j.id
    FROM batch_jobs AS j
    WHERE j.status IN ('validating', 'in_progress', 'cancelling', 'failed', 'expired')
      AND j.phase = 'runnable'
      AND {_supported_kinds_sql("j")}
      AND NOT EXISTS (
          SELECT 1 FROM batch_items AS i
          WHERE i.batch_id = j.id AND i.status IN ('queued', 'running')
      )
      AND (j.status IN ('validating', 'in_progress', 'cancelling') OR j.schema_version >= 2)
    UNION
    SELECT i.batch_id
    FROM batch_items AS i
    JOIN batch_jobs AS j ON j.id = i.batch_id
    WHERE i.status = 'running'
      AND i.lease_expires_at <= now()
      AND j.status IN ('cancelling', 'failed', 'expired')
      AND {_supported_kinds_sql("j")}
"""

# When a batch's results became final, for retention and idempotency expiry:
# its sealing, or (stored before phases existed) its final status.
_ENDED_AT = "COALESCE(j.sealed_at, j.completed_at, j.failed_at, j.expired_at, j.cancelled_at)"

# One batch to purge, oldest first: a finished batch past its retention (its
# own, frozen at creation, else the deployment's), or one whose deletion waited
# for a download. Only kinds this worker supports, never one a download is
# reading (``read_lease_until`` is on the row itself, so a reader that renewed
# after this statement's snapshot makes the locked row fail the recheck), and
# never one with a live upstream call. Its idempotency records keep the expiry
# its end gave them (24 hours after it), so a purge never leaves one behind
# for good. A batch another sweep is purging is skipped.
_PURGE_ONE_SQL = f"""
    WITH victim AS (
        SELECT j.id, {_ENDED_AT} AS ended_at
        FROM batch_jobs AS j
        WHERE (
            (
                j.status IN ('completed', 'failed', 'expired', 'cancelled')
                AND j.phase IN ('runnable', 'sealed')
                AND {_ENDED_AT}
                    < now() - make_interval(
                        secs => COALESCE(j.retention_seconds, $1::bigint * 86400)::double precision)
            )
            OR j.delete_requested_at IS NOT NULL
          )
          AND {_supported_kinds_sql("j")}
          AND (j.read_lease_until IS NULL OR j.read_lease_until <= now())
          AND NOT EXISTS (
              SELECT 1 FROM batch_read_leases AS l
              WHERE l.batch_id = j.id AND l.expires_at > now()
          )
          AND NOT EXISTS (
              SELECT 1 FROM batch_items AS i
              WHERE i.batch_id = j.id AND i.status = 'running' AND i.lease_expires_at > now()
          )
        ORDER BY COALESCE(j.delete_requested_at, {_ENDED_AT})
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    ),
    tombstones AS (
        UPDATE batch_idempotency AS r
        SET deleted_at = COALESCE(r.deleted_at, now()),
            expires_at = COALESCE(
                r.expires_at,
                victim.ended_at + make_interval(secs => {IDEMPOTENCY_RETENTION_SECONDS}),
                now() + make_interval(secs => {IDEMPOTENCY_RETENTION_SECONDS})
            )
        FROM victim
        WHERE r.batch_id = victim.id
        RETURNING r.batch_id
    )
    DELETE FROM batch_jobs WHERE id IN (SELECT id FROM victim)
    RETURNING id
"""

# An idempotency record still counts while its batch is active, for 24 hours
# after the batch ended (or was deleted), and not after.
_IDEMPOTENCY_LIVE = f"""
    COALESCE(
        r.expires_at,
        (SELECT {_ENDED_AT} + make_interval(secs => {IDEMPOTENCY_RETENTION_SECONDS})
         FROM batch_jobs AS j
         WHERE j.id = r.batch_id
           AND j.status IN ('completed', 'failed', 'expired', 'cancelled')
           AND j.phase IN ('runnable', 'sealed')),
        'infinity'::timestamptz
    ) > now()
"""


def new_event_id() -> str:
    """Return a new webhook event id."""
    return f"evt_{secrets.token_hex(16)}"


class BatchStore:
    """Postgres-backed store for batch jobs, their items and per-model caps."""

    def __init__(self, pool: Any) -> None:
        """Wrap an asyncpg pool."""
        self._pool = pool

    @property
    def pool(self) -> Any:
        """Return the asyncpg pool this store and the file and webhook stores share."""
        return self._pool

    async def initialize(self) -> None:
        """Create the batch tables, their new columns and indexes (idempotent).

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
                for name, ddl in _TABLES:
                    if name in missing:
                        await execute_ddl(conn, ddl)
                for table, columns in _COLUMN_MIGRATIONS:
                    present = await existing_columns(conn, table)
                    for column, definition in columns:
                        if column not in present:
                            await execute_ddl(
                                conn,
                                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
                                f"{column} {definition}",
                            )
                for name, ddl in _INDEXES:
                    if name in missing:
                        await execute_ddl(conn, ddl)

    # -- admission ---------------------------------------------------------------

    async def find_idempotent(
        self, *, owner_id: str, api_surface: str, key: str
    ) -> dict[str, Any] | None:
        """Look an ``Idempotency-Key`` up for its owner, outside any lock.

        Returns:
            ``{"batch_id", "request_digest", "deleted"}`` or None when the key
            is unused (or its record has expired).
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT r.batch_id, r.request_digest,
                       (r.deleted_at IS NOT NULL
                        OR NOT EXISTS (SELECT 1 FROM batch_jobs j
                                       WHERE j.id = r.batch_id
                                         AND j.delete_requested_at IS NULL)) AS deleted
                FROM batch_idempotency AS r
                WHERE r.owner_id = $1 AND r.api_surface = $2 AND r.idem_key = $3
                  AND {_IDEMPOTENCY_LIVE}
                """,
                owner_id,
                api_surface,
                key,
            )
        if row is None:
            return None
        return {
            "batch_id": row["batch_id"],
            "request_digest": row["request_digest"],
            "deleted": bool(row["deleted"]),
        }

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
        api_surface: str = SURFACE_OPENAI_INLINE,
        target: str = TARGET_CHAT,
        protocol_profile: str | None = None,
        phase: str = PHASE_RUNNABLE,
        status: str = "validating",
        input_file_id: str | None = None,
        retention_seconds: int | None = None,
        idempotency: dict[str, str] | None = None,
        max_active: int | None = None,
        max_queued: int | None = None,
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
                submission order; item ids follow it. An item carrying an
                ``error`` (``{"code", "message", ...}``) failed admission: it is
                stored already ``failed`` and never claimed.
            metadata: The caller's metadata, or None.
            completion_window_seconds: Time the batch has before its
                unstarted items expire.
            api_surface: ``openai_inline`` or ``openai_file`` (``anthropic`` only
                for a batch stored the way the retired Message Batches API stored them).
            target: ``chat``, ``embeddings``, ``messages`` or ``responses``.
            protocol_profile: The protocol version the batch was created under
                (the ``anthropic_version`` of a Messages batch).
            phase: ``runnable``, or ``validating`` for a file batch whose items
                the validator adds later.
            status: The initial status.
            input_file_id: The uploaded input of a file batch.
            retention_seconds: How long the finished batch is kept, frozen now.
            idempotency: ``{"key", "digest"}`` to record the batch under an
                ``Idempotency-Key``; checked again under the user's lock.
            max_active: The user's cap on unfinished batches, or None.
            max_queued: The user's cap on unfinished requests, or None.

        Returns:
            The batch row. With ``idempotency``, when another create with the
            same key committed first, that batch's row with ``"replayed":
            True`` instead.

        Raises:
            BatchOwnerUnavailable: An authenticated owner no longer exists,
                is not active, or has a hard-delete in progress.
            BatchLimitExceeded: A per-user cap would be exceeded.
            IdempotencyConflict: The key was used for a different request.
            IdempotencyTombstoned: The key's batch was deleted.
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
        models = list(
            dict.fromkeys(
                [*model_ids, *(item["model_id"] for item in items if item.get("error") is None)]
            )
        )
        queued_records = [
            (
                batch_id,
                item["custom_id"],
                item["model_id"],
                _dump_json(item["request"]),
                item.get("line_no"),
            )
            for item in items
            if item.get("error") is None
        ]
        failed_items = [item for item in items if item.get("error") is not None]
        async with self._pool.acquire() as conn, conn.transaction():
            if key_hash is not None:
                owner = await conn.fetchrow(
                    "SELECT status, hard_delete_pending FROM users WHERE id = $1 FOR SHARE",
                    user_id,
                )
                if owner is None or owner["status"] != "active" or owner["hard_delete_pending"]:
                    raise BatchOwnerUnavailable(user_id)
            await conn.execute(_CLAIM_LOCK_SQL, _USER_ADMISSION_LOCK_CLASS, user_id)
            if idempotency is not None:
                replay = await self._idempotent_replay(
                    conn, user_id, api_surface, idempotency["key"], idempotency["digest"]
                )
                if replay is not None:
                    return replay
            await self._check_user_caps(
                conn,
                user_id,
                max_active=max_active,
                max_queued=max_queued,
                requested=len(queued_records),
            )
            if input_file_id is not None:
                await self._pin_input(conn, input_file_id, user_id)
            row = await conn.fetchrow(
                f"""
                INSERT INTO batch_jobs
                    (id, user_id, key_hash, endpoint, model_ids, request_count,
                     metadata, expires_at, api_surface, target, schema_version,
                     protocol_profile, phase, status, input_file_id, retention_seconds,
                     idempotency_key, in_progress_at)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb,
                        now() + make_interval(secs => $8), $9, $10, $11, $12, $13, $14,
                        $15, $16, $17, CASE WHEN $14 = 'in_progress' THEN now() END)
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
                api_surface,
                target,
                SCHEMA_VERSION,
                protocol_profile,
                phase,
                status,
                input_file_id,
                retention_seconds,
                idempotency["key"] if idempotency else None,
            )
            if idempotency is not None:
                await conn.execute(
                    """
                    INSERT INTO batch_idempotency
                        (owner_id, api_surface, idem_key, request_digest, batch_id)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    user_id,
                    api_surface,
                    idempotency["key"],
                    idempotency["digest"],
                    batch_id,
                )
            if queued_records:
                await conn.copy_records_to_table(
                    "batch_items", records=queued_records, columns=_ITEM_COPY_COLUMNS
                )
            if failed_items:
                await self._insert_failed_items(conn, batch_id, failed_items)
                await conn.execute(
                    "UPDATE batch_jobs SET failed_count = failed_count + $2 WHERE id = $1",
                    batch_id,
                    len(failed_items),
                )
            if phase == PHASE_RUNNABLE and (failed_items or not queued_records):
                # Every request may have failed admission: no item will ever
                # report back, so the batch is final now.
                await self._settle(conn, batch_id, status)
                row = await conn.fetchrow(
                    f"SELECT {_BATCH_COLUMNS} FROM batch_jobs WHERE id = $1", batch_id
                )
        return _batch_row(row)

    async def _pin_input(self, conn: Any, file_id: str, user_id: str) -> None:
        """Hold a file batch's input for the batch being created, or refuse it.

        Run in the create transaction. The upload's row is locked ``FOR KEY
        SHARE`` and re-read: a collection holding it (``FOR UPDATE``) is
        waited for, and once it has deleted the row nothing is found and the
        batch is refused. A collection that comes later finds the row locked
        or, after this commits, the validating batch that pins it (see
        :meth:`BatchFileStore.collect_garbage`). The router's earlier check is
        not enough on its own: the input may be deleted or expire in between.

        Raises:
            BatchInputUnavailable: The upload is gone, deleted, expired, not
                ready or not the user's.
        """
        found = await conn.fetchval(
            """
            SELECT id FROM batch_files
            WHERE id = $1 AND user_id = $2 AND kind = 'upload' AND purpose = 'batch'
              AND status = 'ready' AND deleted_at IS NULL
              AND (expires_at IS NULL OR expires_at > now())
            FOR KEY SHARE
            """,
            file_id,
            user_id,
        )
        if found is None:
            raise BatchInputUnavailable(file_id)

    async def _insert_failed_items(
        self, conn: Any, batch_id: str, failed_items: list[dict[str, Any]]
    ) -> None:
        records = [
            (
                batch_id,
                item["custom_id"],
                item["model_id"],
                _dump_json(item["request"]),
                item.get("line_no"),
                "failed",
                _dump_json(item["error"]),
                item["error"].get("status"),
                _utcnow(),
            )
            for item in failed_items
        ]
        await conn.copy_records_to_table(
            "batch_items", records=records, columns=_FAILED_ITEM_COPY_COLUMNS
        )

    async def _idempotent_replay(
        self, conn: Any, owner_id: str, api_surface: str, key: str, digest: str
    ) -> dict[str, Any] | None:
        """Under the user's admission lock: the batch a key already made, or None."""
        record = await conn.fetchrow(
            f"""
            SELECT r.batch_id, r.request_digest, r.deleted_at
            FROM batch_idempotency AS r
            WHERE r.owner_id = $1 AND r.api_surface = $2 AND r.idem_key = $3
              AND {_IDEMPOTENCY_LIVE}
            FOR UPDATE OF r
            """,
            owner_id,
            api_surface,
            key,
        )
        if record is None:
            # An expired record may still be there; it no longer counts.
            await conn.execute(
                "DELETE FROM batch_idempotency "
                "WHERE owner_id = $1 AND api_surface = $2 AND idem_key = $3",
                owner_id,
                api_surface,
                key,
            )
            return None
        if record["request_digest"] != digest:
            raise IdempotencyConflict(record["batch_id"])
        row = await conn.fetchrow(
            f"SELECT {_BATCH_COLUMNS} FROM batch_jobs "
            "WHERE id = $1 AND user_id = $2 AND delete_requested_at IS NULL",
            record["batch_id"],
            owner_id,
        )
        if record["deleted_at"] is not None or row is None:
            raise IdempotencyTombstoned(record["batch_id"])
        return {**_batch_row(row), "replayed": True}

    async def _check_user_caps(
        self,
        conn: Any,
        user_id: str,
        *,
        max_active: int | None,
        max_queued: int | None,
        requested: int,
    ) -> None:
        """Refuse admission past the user's caps; call under the admission lock."""
        if max_active is None and max_queued is None:
            return
        usage = await conn.fetchrow(
            """
            SELECT
                count(*) FILTER (
                    WHERE status IN ('validating', 'in_progress', 'cancelling')
                       OR phase IN ('validating', 'sealing')
                ) AS active,
                COALESCE(sum(GREATEST(request_count - completed_count - failed_count
                                      - cancelled_count, 0)) FILTER (
                    WHERE status IN ('validating', 'in_progress', 'cancelling')
                      AND phase IN ('validating', 'runnable')
                ), 0) AS queued
            FROM batch_jobs
            WHERE user_id = $1 AND delete_requested_at IS NULL
            """,
            user_id,
        )
        if max_active is not None and usage["active"] >= max_active:
            raise BatchLimitExceeded("active_batches", limit=max_active, current=usage["active"])
        if max_queued is not None and usage["queued"] + requested > max_queued:
            raise BatchLimitExceeded(
                "queued_requests",
                limit=max_queued,
                current=int(usage["queued"]),
                requested=requested,
            )

    async def count_active_batches(self, user_id: str) -> int:
        """Count a user's unfinished batches (validating, running, cancelling or sealing)."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT count(*) FROM batch_jobs WHERE user_id = $1 "
                "AND delete_requested_at IS NULL "
                "AND (status IN ('validating', 'in_progress', 'cancelling') "
                "     OR phase IN ('validating', 'sealing'))",
                user_id,
            )

    async def get_batch(
        self, batch_id: str, *, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """Fetch a batch row, or None when absent (or not *user_id*'s, when given)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_BATCH_COLUMNS} FROM batch_jobs "
                "WHERE id = $1 AND ($2::text IS NULL OR user_id = $2) "
                "AND delete_requested_at IS NULL",
                batch_id,
                user_id,
            )
        return _batch_row(row) if row is not None else None

    async def list_batches(
        self,
        *,
        user_id: str | None,
        limit: int,
        after: str | None = None,
        before: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List batches, newest first.

        Args:
            user_id: The owner; None lists every user's (administrators).
            limit: Page size.
            after: Id of the last batch of the previous page: return older
                ones. One that does not exist (any more), or is someone
                else's, ends the listing.
            before: Id of the first batch of the next page: return the newer
                ones just before it (still newest first).
            status: Only batches showing this status (:func:`display_status_sql`,
                the status the API and the console report), not the stored one.

        Returns:
            Up to ``limit`` batch rows ordered by ``created_at DESC, id DESC``.
        """
        conditions = [
            "($1::text IS NULL OR j.user_id = $1)",
            "j.delete_requested_at IS NULL",
            f"($3::text IS NULL OR {display_status_sql('j')} = $3)",
        ]
        args: list[Any] = [user_id, limit, status]
        order = "DESC"
        if after is not None:
            args.append(after)
            conditions.append(
                "(j.created_at, j.id) < (SELECT created_at, id FROM batch_jobs "
                "WHERE id = $4 AND ($1::text IS NULL OR user_id = $1))"
            )
        elif before is not None:
            args.append(before)
            order = "ASC"
            conditions.append(
                "(j.created_at, j.id) > (SELECT created_at, id FROM batch_jobs "
                "WHERE id = $4 AND ($1::text IS NULL OR user_id = $1))"
            )
        columns = ", ".join(f"j.{name.strip()}" for name in _BATCH_COLUMNS.split(","))
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {columns} FROM batch_jobs AS j WHERE {' AND '.join(conditions)} "
                f"ORDER BY j.created_at {order}, j.id {order} LIMIT $2",
                *args,
            )
        result = [_batch_row(row) for row in rows]
        if order == "ASC":
            result.reverse()
        return result

    async def batch_summary(self) -> list[dict[str, Any]]:
        """Per-user totals across every batch still stored (administrators)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id,
                       count(*) AS batches,
                       count(*) FILTER (
                           WHERE status IN ('validating', 'in_progress', 'cancelling')
                              OR phase IN ('validating', 'sealing')
                       ) AS active,
                       COALESCE(sum(request_count), 0) AS requests,
                       COALESCE(sum(completed_count), 0) AS completed,
                       COALESCE(sum(failed_count), 0) AS failed,
                       COALESCE(sum(cost_usd), 0) AS cost_usd,
                       COALESCE(sum(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(sum(completion_tokens), 0) AS completion_tokens,
                       COALESCE(sum(unknown_cost_count), 0) AS unknown_cost_attempts,
                       max(created_at) AS last_created_at
                FROM batch_jobs
                WHERE delete_requested_at IS NULL
                GROUP BY user_id
                ORDER BY max(created_at) DESC
                """
            )
        return [
            {
                "user_id": row["user_id"],
                "batches": row["batches"],
                "active": row["active"],
                "requests": int(row["requests"]),
                "completed": int(row["completed"]),
                "failed": int(row["failed"]),
                "cost_usd": float(row["cost_usd"] or 0),
                "prompt_tokens": int(row["prompt_tokens"]),
                "completion_tokens": int(row["completion_tokens"]),
                "unknown_cost_attempts": int(row["unknown_cost_attempts"]),
                "last_created_at": row["last_created_at"],
            }
            for row in rows
        ]

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
            completed_at, usage, cost_status, attempts, model_id``.
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
        items already running finish, and the batch is finalized as soon as
        none is left (at once when none was running). A batch being validated
        is cancelled by its validator (or the sweep) without running anything.
        A batch in any other status, or already sealing, is left as it is: a
        cancel is idempotent and never changes a result already fixed.

        Returns:
            The batch row as it now stands, or None when absent (or not
            *user_id*'s, when given).
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT status, phase FROM batch_jobs "
                "WHERE id = $1 AND ($2::text IS NULL OR user_id = $2) "
                "AND delete_requested_at IS NULL FOR NO KEY UPDATE",
                batch_id,
                user_id,
            )
            if row is None:
                return None
            if row["status"] in ("validating", "in_progress") and row["phase"] in (
                PHASE_VALIDATING,
                PHASE_RUNNABLE,
            ):
                await conn.execute(
                    "UPDATE batch_jobs SET status = 'cancelling', cancelling_at = now() "
                    "WHERE id = $1",
                    batch_id,
                )
                await self._settle(conn, batch_id, "cancelling")
            batch = await conn.fetchrow(
                f"SELECT {_BATCH_COLUMNS} FROM batch_jobs WHERE id = $1", batch_id
            )
        return _batch_row(batch)

    async def delete_batch(self, batch_id: str, *, user_id: str | None = None) -> bool:
        """Delete a batch with no live upstream call, including its items.

        Scoped to *user_id* when given. The batch's immutable
        model ids are read first, then their claim/heartbeat advisory locks are
        taken in sorted order before the batch is locked and rechecked. A
        heartbeat therefore waits rather than skipping an item lock and losing
        a renewal.

        Its idempotency records become tombstones. While a download is reading
        the batch the rows stay, hidden from every lookup, until the sweep can
        remove them; nothing is ever re-created for a deleted batch.

        Raises:
            BatchDeleteInProgress: At least one item has an unexpired running
                lease and may still be making an upstream call.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT model_ids FROM batch_jobs "
                "WHERE id = $1 AND ($2::text IS NULL OR user_id = $2) "
                "AND delete_requested_at IS NULL",
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
            found = await conn.fetchrow(
                "SELECT id FROM batch_jobs "
                "WHERE id = $1 AND ($2::text IS NULL OR user_id = $2) "
                "AND delete_requested_at IS NULL FOR UPDATE",
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
            await conn.execute(
                """
                UPDATE batch_idempotency
                SET deleted_at = now(),
                    expires_at = GREATEST(COALESCE(expires_at, now()),
                                          now() + make_interval(secs => $2))
                WHERE batch_id = $1
                """,
                batch_id,
                float(IDEMPOTENCY_RETENTION_SECONDS),
            )
            await conn.execute(
                "DELETE FROM batch_webhook_events WHERE batch_id = $1 AND status = 'pending'",
                batch_id,
            )
            reading = await conn.fetchval(
                "SELECT (SELECT read_lease_until > clock_timestamp() FROM batch_jobs WHERE id = $1) "
                "OR EXISTS (SELECT 1 FROM batch_read_leases "
                "WHERE batch_id = $1 AND expires_at > clock_timestamp())",
                batch_id,
            )
            if reading:
                result = await conn.execute(
                    "UPDATE batch_jobs SET delete_requested_at = now() WHERE id = $1", batch_id
                )
            else:
                result = await conn.execute("DELETE FROM batch_jobs WHERE id = $1", batch_id)
        return _rowcount(result) > 0

    async def delete_user_batches(self, user_id: str) -> int:
        """Delete every batch of a user, and everything else batch inference holds for them.

        Used by the admin hard-delete flow: the stored requests and results,
        uploaded files, idempotency records, webhook settings and undelivered
        events are the user's data, owned by neither the operational nor the
        log store. Works before :meth:`initialize` has run (the purge reaches a
        store whose schema migration is still pending), and finds nothing to
        purge where the tables do not exist yet. A worker still holding one of
        these batches finds it gone: every write it attempts is fenced on the
        rows this removes, so nothing is re-created.

        Returns:
            The number of batches deleted.
        """
        async with self._pool.acquire() as conn:
            try:
                async with conn.transaction():
                    await conn.execute(_CLAIM_LOCK_SQL, _USER_ADMISSION_LOCK_CLASS, user_id)
                    result = await conn.execute(
                        """
                        DELETE FROM batch_jobs
                        WHERE id IN (
                            SELECT id FROM batch_jobs WHERE user_id = $1 ORDER BY id FOR UPDATE
                        )
                        """,
                        user_id,
                    )
                    for statement in (
                        "DELETE FROM batch_files WHERE user_id = $1",
                        "DELETE FROM batch_idempotency WHERE owner_id = $1",
                        # The webhook before its events: removing it waits for
                        # any transaction still writing an event under it.
                        "DELETE FROM batch_webhooks WHERE user_id = $1",
                        "DELETE FROM batch_webhook_events WHERE user_id = $1",
                        "DELETE FROM batch_user_state WHERE user_id = $1",
                    ):
                        try:
                            async with conn.transaction():
                                await conn.execute(statement, user_id)
                        except asyncpg.UndefinedTableError:
                            # A table this version adds, not created yet.
                            continue
            except asyncpg.UndefinedTableError:
                return 0
        return _rowcount(result)

    # -- worker ------------------------------------------------------------------

    async def active_models(self) -> list[str]:
        """Return the models of every batch that items may be claimed from now, sorted."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT DISTINCT m.model_id
                FROM batch_jobs AS j
                CROSS JOIN LATERAL jsonb_array_elements_text(j.model_ids) AS m(model_id)
                WHERE {_claimable_job_sql("j")}
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
        ``max_running`` minus that many. Users take turns, then their batches
        (see :data:`_CLAIM_CANDIDATES_SQL`).

        A claimed item becomes ``running`` under a lease of *lease_seconds*,
        with its attempt counted and an attempt record opened; an item taken
        back from a worker whose lease ran out has that worker's attempt
        closed as abandoned. Each batch it came from is marked claimed and
        moves from ``validating`` to ``in_progress``. The batches are locked
        and re-checked first, so a batch cancelled, failed, paused, deleted or
        sealing meanwhile gives nothing.

        Returns:
            The claimed items, in claim order: ``id, batch_id, custom_id,
            model_id, request, attempts`` (counting this one), ``attempt_seq``
            (the fencing token for this attempt), ``error`` (the previous
            attempt's, or None), and the batch's ``user_id``, ``key_hash``,
            ``api_surface``, ``target``, ``protocol_profile`` and ``endpoint``.
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
            candidate_ids = [row["id"] for row in candidates if row["batch_id"] in owners]
            abandoned = await conn.fetch(_CLAIM_EXPIRED_SQL, candidate_ids)
            if abandoned:
                await self._abandon_attempts(
                    conn, [(row["id"], row["attempt_seq"]) for row in abandoned]
                )
            claimed = await conn.fetch(
                _CLAIM_ITEMS_SQL,
                candidate_ids,
                worker_id,
                float(lease_seconds),
                list(owners),
                model_id,
            )
            if not claimed:
                return []
            await conn.execute(
                _OPEN_ATTEMPTS_SQL,
                [row["id"] for row in claimed],
                [row["attempt_seq"] for row in claimed],
                [row["batch_id"] for row in claimed],
                [worker_id] * len(claimed),
            )
            order = {row["id"]: position for position, row in enumerate(candidates)}
            served = sorted(claimed, key=lambda row: order[row["id"]])
            # Whoever got the last slot of this claim goes last next time: a
            # user (and a batch) is placed in the service order by the
            # position of its last item served here.
            last_batch: dict[str, int] = {}
            last_user: dict[str, int] = {}
            for position, row in enumerate(served):
                last_batch[row["batch_id"]] = position
                last_user[owners[row["batch_id"]]["user_id"]] = position
            batches_in_order = sorted(last_batch, key=last_batch.__getitem__)
            users_in_order = sorted(last_user, key=last_user.__getitem__)
            seqs = sorted(
                row["seq"]
                for row in await conn.fetch(
                    _NEXT_SEQ_SQL, len(batches_in_order) + len(users_in_order)
                )
            )
            await conn.execute(
                _CLAIM_TOUCH_BATCHES_SQL,
                batches_in_order,
                seqs[: len(batches_in_order)],
            )
            await conn.execute(
                _CLAIM_TOUCH_USERS_SQL,
                users_in_order,
                seqs[len(batches_in_order) :],
            )
        return [
            {
                "id": row["id"],
                "batch_id": row["batch_id"],
                "custom_id": row["custom_id"],
                "model_id": row["model_id"],
                "request": _load_json(row["request"]),
                "attempts": row["attempts"],
                "attempt_seq": row["attempt_seq"],
                "error": _load_json(row["error"]),
                "user_id": owners[row["batch_id"]]["user_id"],
                "key_hash": owners[row["batch_id"]]["key_hash"],
                "api_surface": owners[row["batch_id"]]["api_surface"],
                "target": owners[row["batch_id"]]["target"],
                "protocol_profile": owners[row["batch_id"]]["protocol_profile"],
                "endpoint": owners[row["batch_id"]]["endpoint"],
            }
            for row in served
        ]

    async def _abandon_attempts(self, conn: Any, attempts: list[tuple[int, int]]) -> None:
        """Close attempts whose worker vanished: unknown cost once dispatched."""
        if not attempts:
            return
        rows = await conn.fetch(
            """
            UPDATE batch_attempts AS a
            SET outcome = 'abandoned', finished_at = now(),
                cost_status = CASE WHEN a.dispatched_at IS NULL THEN 'not_billed'
                                   ELSE 'unknown' END
            FROM unnest($1::bigint[], $2::int[]) AS t(item_id, attempt_no)
            WHERE a.item_id = t.item_id AND a.attempt_no = t.attempt_no AND a.outcome IS NULL
            RETURNING a.item_id, a.batch_id, a.cost_status
            """,
            [item_id for item_id, _ in attempts],
            [attempt_no for _, attempt_no in attempts],
        )
        unknown = [row for row in rows if row["cost_status"] == "unknown"]
        if not unknown:
            return
        await conn.execute(
            "UPDATE batch_items SET cost_status = 'unknown' WHERE id = ANY($1::bigint[])",
            [row["item_id"] for row in unknown],
        )
        per_batch: dict[str, int] = {}
        for row in unknown:
            per_batch[row["batch_id"]] = per_batch.get(row["batch_id"], 0) + 1
        for batch_id, count in sorted(per_batch.items()):
            await conn.execute(
                "UPDATE batch_jobs SET unknown_cost_count = unknown_cost_count + $2 WHERE id = $1",
                batch_id,
                count,
            )

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

    async def mark_dispatched(self, *, item_id: int, worker_id: str, attempt: int) -> bool:
        """Record that an attempt's upstream call is about to begin.

        From here on, an attempt that never reports back has an unknown cost
        rather than none.
        """
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE batch_attempts SET dispatched_at = now()
                WHERE item_id = $1 AND attempt_no = $3 AND outcome IS NULL
                  AND EXISTS (SELECT 1 FROM batch_items
                              WHERE id = $1 AND status = 'running' AND claimed_by = $2
                                AND attempt_seq = $3)
                """,
                item_id,
                worker_id,
                attempt,
            )
        return _rowcount(result) > 0

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
        usage: dict[str, Any] | None = None,
        cost_status: str | None = None,
        max_result_bytes: int | None = None,
    ) -> bool:
        """Record an item's final outcome: ``completed`` without *error*, else ``failed``.

        In the same transaction the attempt is closed with what it was billed,
        the batch counts the item as completed or failed, adds the attempt's
        known cost and tokens (an unknown cost is counted as unknown, never as
        zero), and settles.

        Args:
            attempt: The item's ``attempt_seq`` at the claim (its fencing token).
            cost_status: ``billed`` (``cost_usd`` was charged), ``not_billed``
                or ``unknown``. Derived when None: billed with a cost, else
                unknown for a success and not billed for an error.
            max_result_bytes: A response larger than this, serialized, is not
                stored: the item fails with ``result_too_large`` instead (its
                cost still counts).

        Returns:
            False, changing nothing, when the item is no longer running under
            this worker's claim at this attempt (its lease was lost, or the
            batch was deleted).
        """
        # A NaN would make the batch's running total NaN for good.
        if cost_usd is not None and not math.isfinite(cost_usd):
            cost_usd = None
        if cost_status is None:
            if cost_usd is not None:
                cost_status = "billed"
            else:
                cost_status = "unknown" if error is None else "not_billed"
        if cost_status == "unknown":
            cost_usd = None
        stored_response = _dump_json(response)
        if (
            max_result_bytes is not None
            and stored_response is not None
            and len(stored_response.encode("utf-8")) > max_result_bytes
        ):
            stored_response = None
            response = None
            error = {
                "code": "result_too_large",
                "message": f"The response exceeds the batch result limit of {max_result_bytes} bytes.",
                "status": 500,
            }
            status_code = 500
        status = "completed" if error is None else "failed"
        async with self._pool.acquire() as conn, conn.transaction():
            batch = await self._guarded_item_write(
                conn,
                _COMPLETE_ITEM_SQL,
                item_id,
                worker_id,
                attempt,
                status,
                stored_response,
                _dump_json(error),
                status_code,
                prompt_tokens,
                completion_tokens,
                cost_usd,
                request_id,
                _dump_json(usage),
                cost_status,
            )
            if batch is None:
                return False
            batch_id, batch_status, _owner = batch
            await conn.execute(
                _CLOSE_ATTEMPT_SQL,
                item_id,
                attempt,
                "succeeded" if error is None else "failed",
                status_code,
                request_id,
                _dump_json(usage),
                cost_usd,
                cost_status,
                _dump_json(error),
            )
            await conn.execute(
                """
                UPDATE batch_jobs
                SET completed_count = completed_count + $2,
                    failed_count = failed_count + $3,
                    cost_usd = cost_usd + $4,
                    prompt_tokens = prompt_tokens + $5,
                    completion_tokens = completion_tokens + $6,
                    unknown_cost_count = unknown_cost_count + $7
                WHERE id = $1
                """,
                batch_id,
                int(error is None),
                int(error is not None),
                float(cost_usd or 0.0),
                int(prompt_tokens or 0),
                int(completion_tokens or 0),
                int(cost_status == "unknown"),
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
        cost_usd: float | None = None,
        cost_status: str | None = None,
        status_code: int | None = None,
        request_id: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> bool:
        """Queue an item again after a retryable failure, keeping *error* as its last.

        The attempt stays counted and is closed with what it was billed. The
        batch settles in the same transaction, so the item of a batch
        cancelled, failed or expired meanwhile takes the batch's verdict
        instead of running again.

        Returns:
            False, changing nothing, when the item is no longer running under
            this worker's claim at this attempt.
        """
        if cost_usd is not None and not math.isfinite(cost_usd):
            cost_usd = None
        if cost_status is None:
            cost_status = "billed" if cost_usd is not None else "not_billed"
        if cost_status == "unknown":
            cost_usd = None
        async with self._pool.acquire() as conn, conn.transaction():
            batch = await self._guarded_item_write(
                conn,
                _RETRY_ITEM_SQL,
                item_id,
                worker_id,
                attempt,
                next_attempt_at,
                _dump_json(error),
                cost_usd,
                cost_status,
            )
            if batch is None:
                return False
            batch_id, batch_status, _owner = batch
            await conn.execute(
                _CLOSE_ATTEMPT_SQL,
                item_id,
                attempt,
                "retry",
                status_code,
                request_id,
                _dump_json(usage),
                cost_usd,
                cost_status,
                _dump_json(error),
            )
            if cost_usd or cost_status == "unknown":
                await conn.execute(
                    "UPDATE batch_jobs SET cost_usd = cost_usd + $2, "
                    "unknown_cost_count = unknown_cost_count + $3 WHERE id = $1",
                    batch_id,
                    float(cost_usd or 0.0),
                    int(cost_status == "unknown"),
                )
            await self._settle(conn, batch_id, batch_status)
        return True

    async def release_item(
        self,
        *,
        item_id: int,
        worker_id: str,
        attempt: int,
        next_attempt_at: datetime | None = None,
        dispatched: bool = False,
    ) -> bool:
        """Queue an item again without spending its attempt (the claim is handed back).

        Args:
            item_id: The item.
            worker_id: The claiming worker.
            attempt: The item's ``attempt_seq`` at the claim.
            next_attempt_at: When it may be claimed again; None for at once.
            dispatched: The attempt's upstream call had begun (a stop that
                cancelled it): its cost is unknown, and recorded as such.

        Returns:
            False, changing nothing, when the item is no longer running under
            this worker's claim at this attempt.
        """
        cost_status = "unknown" if dispatched else "not_billed"
        async with self._pool.acquire() as conn, conn.transaction():
            batch = await self._guarded_item_write(
                conn,
                _RELEASE_ITEM_SQL,
                item_id,
                worker_id,
                attempt,
                next_attempt_at,
                cost_status,
            )
            if batch is None:
                return False
            batch_id, batch_status, _owner = batch
            await conn.execute(
                _CLOSE_ATTEMPT_SQL,
                item_id,
                attempt,
                "abandoned" if dispatched else "released",
                None,
                None,
                None,
                None,
                cost_status,
                None,
            )
            if dispatched:
                await conn.execute(
                    "UPDATE batch_jobs SET unknown_cost_count = unknown_cost_count + 1 "
                    "WHERE id = $1",
                    batch_id,
                )
            await self._settle(conn, batch_id, batch_status)
        return True

    async def fail_batch(self, batch_id: str, error: dict[str, Any]) -> bool:
        """Fail a validating or in-progress batch (its owner's key is no longer valid).

        Its queued items fail as ``batch_failed`` and count as failed; items
        already running finish normally.

        Returns:
            True when the batch was failed, False when absent or in another
            status (a cancel in progress wins, and so does sealing).
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT status, phase FROM batch_jobs WHERE id = $1 FOR NO KEY UPDATE", batch_id
            )
            if row is None or row["status"] not in ("validating", "in_progress"):
                return False
            if row["phase"] not in (PHASE_RUNNABLE, PHASE_VALIDATING):
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
        """Expire, finalize, purge and tidy up; safe to run from several processes at once.

        1. A validating or in-progress batch past ``expires_at`` becomes
           ``expired``; its queued items expire as ``batch_expired`` and count
           as failed. Items already running finish normally. A batch already
           sealing is not touched: its result is fixed.
        2. An active batch with no open item left is finalized, and a batch no
           longer claimable from passes its verdict on to running items whose
           lease ran out.
        3. A finished batch past its retention, or one whose deletion waited
           for a download, is deleted with its items (at most ``_PURGE_LIMIT``
           per sweep).
        4. Expired idempotency records, read leases and old interactive-load
           rows are removed; the idempotency records of finished batches get
           their expiry.

        Returns:
            ``{"expired": n, "finalized": n, "purged": n}`` (batches).
        """
        counts = {"expired": 0, "finalized": 0, "purged": 0}
        async with self._pool.acquire() as conn:
            overdue = await conn.fetch(
                "SELECT j.id FROM batch_jobs AS j "
                "WHERE j.status IN ('validating', 'in_progress') "
                "AND j.phase IN ('validating', 'runnable') "
                f"AND j.expires_at <= now() AND {_supported_kinds_sql('j')} ORDER BY j.id"
            )
            for row in overdue:
                async with conn.transaction():
                    status = await conn.fetchval(
                        "SELECT status FROM batch_jobs WHERE id = $1 "
                        "AND status IN ('validating', 'in_progress') "
                        "AND phase IN ('validating', 'runnable') AND expires_at <= now() "
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

            # Records whose batch ended get the expiry its end gives them;
            # records whose batch vanished without one (a purge by an older
            # version) expire now.
            await conn.execute(
                f"""
                UPDATE batch_idempotency AS r
                SET expires_at = {_ENDED_AT} + make_interval(secs => $1)
                FROM batch_jobs AS j
                WHERE r.batch_id = j.id AND r.expires_at IS NULL
                  AND j.status IN ('completed', 'failed', 'expired', 'cancelled')
                  AND j.phase IN ('runnable', 'sealed')
                """,
                float(IDEMPOTENCY_RETENTION_SECONDS),
            )
            await conn.execute(
                "UPDATE batch_idempotency AS r SET expires_at = now() "
                "WHERE r.expires_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM batch_jobs AS j WHERE j.id = r.batch_id)"
            )
            await conn.execute(
                "DELETE FROM batch_idempotency WHERE expires_at IS NOT NULL AND expires_at <= now()"
            )
            await conn.execute("DELETE FROM batch_read_leases WHERE expires_at <= now()")
            await conn.execute(
                "DELETE FROM batch_interactive_load WHERE updated_at < now() - interval '10 minutes'"
            )
        return counts

    # -- validation and sealing (work leases on the batch row) ------------------

    async def claim_batch_work(
        self, *, phase: str, worker_id: str, lease_seconds: int, limit: int = 1
    ) -> list[dict[str, Any]]:
        """Take the work lease of batches waiting in *phase* (``validating`` or ``sealing``).

        A batch whose previous holder's lease ran out is taken over. Sealing
        bumps ``seal_generation``, so only the newest holder can publish.

        Returns:
            The batch rows taken, with ``seal_generation`` as now leased.
        """
        if phase not in (PHASE_VALIDATING, PHASE_SEALING):
            raise ValueError(f"no work in phase {phase!r}")
        status_filter = "AND j.status = 'validating'" if phase == PHASE_VALIDATING else ""
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                f"""
                UPDATE batch_jobs AS j
                SET work_owner = $2,
                    work_expires_at = now() + make_interval(secs => $3),
                    seal_generation = j.seal_generation + CASE WHEN $1 = 'sealing' THEN 1 ELSE 0 END
                WHERE j.id IN (
                    SELECT j.id FROM batch_jobs AS j
                    WHERE j.phase = $1 {status_filter}
                      AND j.delete_requested_at IS NULL
                      AND (j.work_expires_at IS NULL OR j.work_expires_at <= now())
                      AND {_supported_kinds_sql("j")}
                    ORDER BY j.created_at, j.id
                    LIMIT $4
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING {", ".join(f"j.{c.strip()}" for c in _BATCH_COLUMNS.split(","))},
                          j.seal_generation, j.retention_seconds
                """,
                phase,
                worker_id,
                float(lease_seconds),
                limit,
            )
        result = []
        for row in rows:
            batch = _batch_row(row)
            batch["seal_generation"] = row["seal_generation"]
            batch["retention_seconds"] = row["retention_seconds"]
            result.append(batch)
        return result

    async def extend_batch_work(
        self, *, batch_id: str, worker_id: str, lease_seconds: int, phase: str
    ) -> bool:
        """Extend a work lease; False when it is no longer this worker's (or the phase moved on)."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE batch_jobs SET work_expires_at = now() + make_interval(secs => $3) "
                "WHERE id = $1 AND work_owner = $2 AND phase = $4 "
                "AND delete_requested_at IS NULL",
                batch_id,
                worker_id,
                float(lease_seconds),
                phase,
            )
        return _rowcount(result) > 0

    async def validation_status(self, batch_id: str, worker_id: str) -> str | None:
        """The status of a batch this worker validates, or None once it lost the lease."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT status FROM batch_jobs WHERE id = $1 AND work_owner = $2 "
                "AND phase = 'validating' AND delete_requested_at IS NULL",
                batch_id,
                worker_id,
            )

    async def reset_staged_items(self, batch_id: str, worker_id: str) -> bool:
        """Remove what an earlier validation of a batch staged, before starting over."""
        async with self._pool.acquire() as conn, conn.transaction():
            owned = await conn.fetchval(
                "SELECT TRUE FROM batch_jobs WHERE id = $1 AND work_owner = $2 "
                "AND phase = 'validating' FOR NO KEY UPDATE",
                batch_id,
                worker_id,
            )
            if not owned:
                return False
            await conn.execute("DELETE FROM batch_items WHERE batch_id = $1", batch_id)
            await conn.execute(
                "UPDATE batch_jobs SET request_count = 0, model_ids = '[]'::jsonb WHERE id = $1",
                batch_id,
            )
        return True

    async def stage_items(
        self,
        *,
        batch_id: str,
        worker_id: str,
        user_id: str,
        items: list[dict[str, Any]],
        max_queued: int | None,
    ) -> bool:
        """Add a chunk of validated items to a batch still being validated.

        Under the user's admission lock the chunk is checked against the
        user's queued-request cap and reserved by raising the batch's
        ``request_count``. Nothing here is claimable: the batch stays in phase
        ``validating`` until :meth:`publish_validated`.

        Returns:
            False when the batch is no longer this worker's to validate (or no
            longer ``validating``): nothing was staged.

        Raises:
            BatchLimitExceeded: The user's queued-request cap would be exceeded.
        """
        records = [
            (
                batch_id,
                item["custom_id"],
                item["model_id"],
                _dump_json(item["request"]),
                item.get("line_no"),
            )
            for item in items
        ]
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(_CLAIM_LOCK_SQL, _USER_ADMISSION_LOCK_CLASS, user_id)
            owned = await conn.fetchval(
                "SELECT TRUE FROM batch_jobs WHERE id = $1 AND work_owner = $2 "
                "AND phase = 'validating' AND status = 'validating' "
                "AND delete_requested_at IS NULL FOR NO KEY UPDATE",
                batch_id,
                worker_id,
            )
            if not owned:
                return False
            await self._check_user_caps(
                conn, user_id, max_active=None, max_queued=max_queued, requested=len(records)
            )
            if records:
                await conn.copy_records_to_table(
                    "batch_items", records=records, columns=_ITEM_COPY_COLUMNS
                )
            await conn.execute(
                "UPDATE batch_jobs SET request_count = request_count + $2, "
                "work_expires_at = GREATEST(work_expires_at, now() + interval '60 seconds') "
                "WHERE id = $1",
                batch_id,
                len(records),
            )
        return True

    async def publish_validated(
        self, *, batch_id: str, worker_id: str, model_ids: list[str]
    ) -> dict[str, Any] | None:
        """Make a validated batch runnable: flip the phase, nothing else.

        Returns:
            The batch row, or None when the batch is no longer this worker's
            to validate or was cancelled, expired or failed meanwhile.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"""
                UPDATE batch_jobs
                SET phase = 'runnable', status = 'in_progress', in_progress_at = now(),
                    model_ids = $3::jsonb, work_owner = NULL, work_expires_at = NULL
                WHERE id = $1 AND work_owner = $2 AND phase = 'validating'
                  AND status = 'validating' AND delete_requested_at IS NULL
                RETURNING {_BATCH_COLUMNS}
                """,
                batch_id,
                worker_id,
                json.dumps(sorted(set(model_ids))),
            )
            if row is None:
                return None
            if not await conn.fetchval(_HAS_OPEN_ITEMS_SQL, batch_id):
                await self._settle(conn, batch_id, "in_progress")
                row = await conn.fetchrow(
                    f"SELECT {_BATCH_COLUMNS} FROM batch_jobs WHERE id = $1", batch_id
                )
        return _batch_row(row)

    async def fail_validation(
        self, *, batch_id: str, worker_id: str | None, errors: dict[str, Any]
    ) -> bool:
        """Fail a batch whose input did not validate: nothing of it ever runs.

        Its staged items are removed, it counts no request, and it is final
        (``failed``) with ``validation_error`` holding the problems found. A
        batch cancelled or expired while it was validated ends that way
        instead (``errors`` is then ignored). With ``worker_id`` None, as from
        the sweep, any lease is taken over.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT status FROM batch_jobs WHERE id = $1 AND phase = 'validating' "
                "AND ($2::text IS NULL OR work_owner = $2) FOR NO KEY UPDATE",
                batch_id,
                worker_id,
            )
            if row is None:
                return False
            await conn.execute("DELETE FROM batch_items WHERE batch_id = $1", batch_id)
            final = {"validating": "failed", "cancelling": "cancelled", "expired": "expired"}.get(
                row["status"], "failed"
            )
            # The status itself moves when the batch is sealed, like every
            # other file batch; until then it shows as finalizing/cancelling.
            await conn.execute(
                """
                UPDATE batch_jobs
                SET request_count = 0, completed_count = 0, failed_count = 0,
                    cancelled_count = 0, expired_count = 0,
                    validation_error = CASE WHEN $2 = 'failed' THEN $3::jsonb END,
                    phase = 'runnable', work_owner = NULL, work_expires_at = NULL
                WHERE id = $1
                """,
                batch_id,
                final,
                _dump_json(errors),
            )
            await self._finalize(conn, batch_id, final)
        return True

    async def stale_validations(self) -> list[str]:
        """Batches in validation that were cancelled or expired and whose validator is gone."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT j.id FROM batch_jobs AS j
                WHERE j.phase = 'validating' AND j.status <> 'validating'
                  AND (j.work_expires_at IS NULL OR j.work_expires_at <= now())
                  AND {_supported_kinds_sql("j")}
                ORDER BY j.id
                """
            )
        return [row["id"] for row in rows]

    async def iter_items(
        self, batch_id: str, *, after: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        """A page of a batch's items for rendering, in submission order (by id)."""
        return await self.list_results(batch_id, after=after, limit=limit)

    async def commit_seal(
        self,
        *,
        batch_id: str,
        worker_id: str,
        generation: int,
        files: list[dict[str, Any]],
        event: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Publish a sealed file batch: its output manifests and final status, at once.

        Applies only while the batch is still ``sealing`` under this worker's
        lease and generation; otherwise nothing changes (another worker
        sealed, or the batch was deleted). Any manifest of an older generation
        is removed in the same transaction, so exactly one set is valid.

        Args:
            files: ``{"id", "kind" ("output"|"error"), "filename", "bytes",
                "sha256", "line_count", "renderer_version"}`` per file.
            event: A webhook event ``{"id", "type", "payload"}`` to enqueue,
                for an owner with a webhook configured.

        Returns:
            The batch row now sealed, or None.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT user_id, pending_terminal_status, retention_seconds FROM batch_jobs "
                "WHERE id = $1 AND phase = 'sealing' AND work_owner = $2 "
                "AND seal_generation = $3 AND delete_requested_at IS NULL FOR NO KEY UPDATE",
                batch_id,
                worker_id,
                generation,
            )
            if row is None:
                return None
            await conn.execute(
                "DELETE FROM batch_files WHERE batch_id = $1 AND kind IN ('output', 'error')",
                batch_id,
            )
            output_id = error_id = None
            for spec in files:
                await conn.execute(
                    """
                    INSERT INTO batch_files
                        (id, user_id, purpose, kind, filename, status, bytes, sha256,
                         line_count, batch_id, generation, renderer_version, ready_at)
                    VALUES ($1, $2, 'batch_output', $3, $4, 'ready', $5, $6, $7, $8, $9, $10, now())
                    """,
                    spec["id"],
                    row["user_id"],
                    spec["kind"],
                    spec["filename"],
                    int(spec["bytes"]),
                    spec["sha256"],
                    int(spec["line_count"]),
                    batch_id,
                    generation,
                    int(spec["renderer_version"]),
                )
                if spec["kind"] == "output":
                    output_id = spec["id"]
                else:
                    error_id = spec["id"]
            final = row["pending_terminal_status"] or "completed"
            column = _STATUS_TIMESTAMP[final]
            sealed = await conn.fetchrow(
                f"""
                UPDATE batch_jobs
                SET phase = 'sealed', status = $2, sealed_at = now(),
                    {column} = COALESCE({column}, now()),
                    output_file_id = $3, error_file_id = $4,
                    results_expires_at = now() + make_interval(
                        secs => COALESCE(retention_seconds, $5)::double precision),
                    work_owner = NULL, work_expires_at = NULL
                WHERE id = $1
                RETURNING {_BATCH_COLUMNS}
                """,
                batch_id,
                final,
                output_id,
                error_id,
                float(7 * 86400),
            )
            if event is not None:
                await _enqueue_event(conn, row["user_id"], batch_id, event)
        return _batch_row(sealed)

    async def seal_backlog(self) -> int:
        """How many batches are waiting to be sealed (for the error metric)."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT count(*) FROM batch_jobs WHERE phase = 'sealing'")

    # -- shared interactive load ------------------------------------------------

    async def publish_interactive_load(self, *, process_id: str, load: dict[str, int]) -> None:
        """Replace this process's row set in the shared interactive-load view."""
        models = sorted(load)
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM batch_interactive_load WHERE process_id = $1 "
                "AND NOT (model_id = ANY($2::text[]))",
                process_id,
                models,
            )
            await conn.execute(
                """
                INSERT INTO batch_interactive_load (process_id, model_id, in_flight, updated_at)
                SELECT $1, m, n, now() FROM unnest($2::text[], $3::int[]) AS t(m, n)
                ON CONFLICT (process_id, model_id) DO UPDATE SET
                    in_flight = EXCLUDED.in_flight, updated_at = EXCLUDED.updated_at
                """,
                process_id,
                models,
                [int(load[m]) for m in models],
            )
            # A heartbeat row, so an idle process still shows it is alive.
            await conn.execute(
                """
                INSERT INTO batch_interactive_load (process_id, model_id, in_flight, updated_at)
                VALUES ($1, '', 0, now())
                ON CONFLICT (process_id, model_id) DO UPDATE SET updated_at = now()
                """,
                process_id,
            )

    async def interactive_load(
        self, *, ttl_seconds: float, exclude_process: str | None = None
    ) -> dict[str, int]:
        """Interactive requests in flight per model, from fresh rows only, in one snapshot.

        ``exclude_process`` leaves one process's rows out (the reader's own,
        which it replaces with its live count).
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT model_id, sum(in_flight)::int AS in_flight
                FROM batch_interactive_load
                WHERE updated_at > now() - make_interval(secs => $1) AND model_id <> ''
                  AND ($2::text IS NULL OR process_id <> $2)
                GROUP BY model_id
                """,
                float(ttl_seconds),
                exclude_process,
            )
        return {row["model_id"]: row["in_flight"] for row in rows}

    async def withdraw_interactive_load(self, process_id: str) -> None:
        """Remove this process's rows (a clean stop)."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM batch_interactive_load WHERE process_id = $1", process_id
            )

    # -- read leases ---------------------------------------------------------------

    async def take_read_lease(
        self, *, batch_id: str | None = None, file_id: str | None = None, ttl_seconds: int
    ) -> str:
        """Register a download in progress; deletion of what it reads waits for it.

        The lease is stamped on the resource row itself (``read_lease_until``)
        in the same transaction as the lease record. Taking it updates that
        row, so it waits for a deletion holding the row and then finds it gone,
        and a deletion that chose the row from an older snapshot sees the
        newer lease when it locks the row, and skips it.

        Raises:
            LookupError: The batch (or file) is gone or being deleted.
        """
        lease_id = secrets.token_hex(12)
        async with self._pool.acquire() as conn, conn.transaction():
            if batch_id is not None:
                found = await conn.fetchval(
                    "UPDATE batch_jobs SET read_lease_until = GREATEST("
                    "COALESCE(read_lease_until, now()), now() + make_interval(secs => $2)) "
                    "WHERE id = $1 AND delete_requested_at IS NULL RETURNING id",
                    batch_id,
                    float(ttl_seconds),
                )
            else:
                found = await conn.fetchval(
                    "UPDATE batch_files SET read_lease_until = GREATEST("
                    "COALESCE(read_lease_until, now()), now() + make_interval(secs => $2)) "
                    "WHERE id = $1 RETURNING id",
                    file_id,
                    float(ttl_seconds),
                )
            if found is None:
                raise LookupError(batch_id or file_id)
            await conn.execute(
                "INSERT INTO batch_read_leases (lease_id, batch_id, file_id, expires_at) "
                "VALUES ($1, $2, $3, now() + make_interval(secs => $4))",
                lease_id,
                batch_id,
                file_id,
                float(ttl_seconds),
            )
        return lease_id

    async def renew_read_lease(self, lease_id: str, *, ttl_seconds: int) -> bool:
        """Keep a download's lease alive; False when it is gone (or its resource is)."""
        async with self._pool.acquire() as conn, conn.transaction():
            lease = await conn.fetchrow(
                "UPDATE batch_read_leases SET expires_at = now() + make_interval(secs => $2) "
                "WHERE lease_id = $1 RETURNING batch_id, file_id",
                lease_id,
                float(ttl_seconds),
            )
            if lease is None:
                return False
            if lease["batch_id"] is not None:
                found = await conn.fetchval(
                    "UPDATE batch_jobs SET read_lease_until = GREATEST("
                    "COALESCE(read_lease_until, now()), now() + make_interval(secs => $2)) "
                    "WHERE id = $1 RETURNING id",
                    lease["batch_id"],
                    float(ttl_seconds),
                )
            else:
                found = await conn.fetchval(
                    "UPDATE batch_files SET read_lease_until = GREATEST("
                    "COALESCE(read_lease_until, now()), now() + make_interval(secs => $2)) "
                    "WHERE id = $1 RETURNING id",
                    lease["file_id"],
                    float(ttl_seconds),
                )
            return found is not None

    async def drop_read_lease(self, lease_id: str) -> None:
        """End a download's lease (the row's horizon simply lapses)."""
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM batch_read_leases WHERE lease_id = $1", lease_id)

    async def batch_exists(self, batch_id: str) -> bool:
        """Whether the batch's rows are still stored (deleted-but-read ones included)."""
        async with self._pool.acquire() as conn:
            return bool(await conn.fetchval("SELECT TRUE FROM batch_jobs WHERE id = $1", batch_id))

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
    ) -> tuple[str, str, str] | None:
        """Lock an item's batch, then run a worker write carrying ``_ITEM_GUARD``.

        Returns:
            ``(batch_id, batch status, owner)`` when the write applied, None
            when the item or its batch is gone or the guard did not match.
        """
        batch = await conn.fetchrow(_LOCK_ITEM_BATCH_SQL, item_id)
        if batch is None:
            return None
        result = await conn.execute(sql, item_id, worker_id, attempt, *args)
        if _rowcount(result) == 0:
            return None
        return batch["id"], batch["status"], batch["user_id"]

    async def _settle(self, conn: Any, batch_id: str, status: str) -> str | None:
        """Bring a batch's open items in line with its *status*; finalize it when none is left.

        Must run inside the caller's transaction, holding the batch row's lock.
        A batch still validating is never touched here: its items are staged,
        not open, and its validator decides.

        Returns:
            The final status the batch moved to, or None when it did not move.
        """
        phase_row = await conn.fetchrow(
            "SELECT phase, schema_version FROM batch_jobs WHERE id = $1", batch_id
        )
        if phase_row is None or phase_row["phase"] != PHASE_RUNNABLE:
            return None
        verdict = _ITEM_VERDICTS.get(status)
        if verdict is not None:
            stale = await conn.fetch(_EXPIRED_RUNNING_SQL, batch_id)
            if stale:
                await self._abandon_attempts(
                    conn, [(row["id"], row["attempt_seq"]) for row in stale]
                )
            item_status, error = verdict
            settled = _rowcount(
                await conn.execute(_SETTLE_ITEMS_SQL, batch_id, item_status, _dump_json(error))
            )
            if settled:
                if status == "cancelling":
                    await conn.execute(
                        "UPDATE batch_jobs SET cancelled_count = cancelled_count + $2 WHERE id = $1",
                        batch_id,
                        settled,
                    )
                else:
                    await conn.execute(
                        "UPDATE batch_jobs SET failed_count = failed_count + $2, "
                        "expired_count = expired_count + $3 WHERE id = $1",
                        batch_id,
                        settled,
                        settled if status == "expired" else 0,
                    )
        final = _FINAL_STATUS.get(status)
        if final is None or await conn.fetchval(_HAS_OPEN_ITEMS_SQL, batch_id):
            return None
        if status in ("failed", "expired") and phase_row["schema_version"] < 2:
            # A batch stored before phases existed is already final in these
            # statuses; there is nothing to seal.
            return None
        await self._finalize(conn, batch_id, final)
        return final

    async def _finalize(self, conn: Any, batch_id: str, final: str) -> None:
        """Fix a batch's final status: seal it now, or hand it to a sealer (file batches)."""
        row = await conn.fetchrow(
            "SELECT user_id, api_surface, status, phase, retention_seconds, request_count, "
            "completed_count, failed_count, cancelled_count, expired_count, "
            "created_at, expires_at FROM batch_jobs WHERE id = $1",
            batch_id,
        )
        if row is None or row["phase"] in (PHASE_SEALING, PHASE_SEALED):
            return
        if row["api_surface"] == SURFACE_OPENAI_FILE:
            await conn.execute(
                "UPDATE batch_jobs SET phase = 'sealing', pending_terminal_status = $2, "
                "finalizing_at = now(), work_owner = NULL, work_expires_at = NULL WHERE id = $1",
                batch_id,
                final,
            )
            return
        column = _STATUS_TIMESTAMP[final]
        await conn.execute(
            f"""
            UPDATE batch_jobs
            SET status = $2, {column} = COALESCE({column}, now()),
                phase = 'sealed', sealed_at = now(), pending_terminal_status = $2,
                results_expires_at = now() + make_interval(
                    secs => COALESCE(retention_seconds, $3)::double precision)
            WHERE id = $1
            """,
            batch_id,
            final,
            float(7 * 86400),
        )
        await _enqueue_event(
            conn,
            row["user_id"],
            batch_id,
            terminal_event(batch_id, final),
        )


def terminal_event(batch_id: str, final_status: str) -> dict[str, Any]:
    """The webhook event a batch's end publishes. Never carries prompts or results.

    ``batch.<final status>`` for every batch, a legacy Message Batch included.
    Events already in the outbox are delivered as they were written.
    """
    import time

    event_id = new_event_id()
    event_type = f"batch.{final_status}"
    return {
        "id": event_id,
        "type": event_type,
        "payload": {
            "id": event_id,
            "object": "event",
            "type": event_type,
            "created_at": int(time.time()),
            "data": {"id": batch_id},
        },
    }


async def _enqueue_event(conn: Any, user_id: str, batch_id: str, event: dict[str, Any]) -> None:
    """Add a webhook event to the outbox, when the user has an enabled webhook.

    The webhook row is locked (``FOR KEY SHARE``) until the caller's
    transaction commits, so a concurrent removal of the webhook waits for this
    event and then removes it too: an event is never left behind without the
    webhook it belongs to. A removal that committed first leaves no row to
    lock, and no event is written.
    """
    try:
        enabled = await conn.fetchval(
            "SELECT enabled FROM batch_webhooks WHERE user_id = $1 FOR KEY SHARE", user_id
        )
        if not enabled:
            return
        await conn.execute(
            """
            INSERT INTO batch_webhook_events (id, user_id, batch_id, event_type, payload)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            """,
            event["id"],
            user_id,
            batch_id,
            event["type"],
            json.dumps(event["payload"]),
        )
    except asyncpg.UndefinedTableError:  # pragma: no cover - schema always created together
        return


def _utcnow() -> Any:
    """``completed_at`` for an item that failed admission."""
    from datetime import UTC, datetime

    return datetime.now(UTC)


async def _missing_schema_objects(conn: Any) -> set[str]:
    """Names of the batch tables, indexes and columns the database does not have yet.

    One catalog query: a missing table or index by name, a missing column as
    ``table.column`` (of a table that exists; a missing table brings its own).
    """
    names = [name for name, _ in (*_TABLES, *_INDEXES)]
    tables = [table for table, columns in _COLUMN_MIGRATIONS for _ in columns]
    columns = [column for _, cols in _COLUMN_MIGRATIONS for column, _ in cols]
    rows = await conn.fetch(
        """
        SELECT name FROM unnest($1::text[]) AS name WHERE to_regclass(name) IS NULL
        UNION ALL
        SELECT t.tbl || '.' || t.col
        FROM unnest($2::text[], $3::text[]) AS t(tbl, col)
        WHERE to_regclass(t.tbl) IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM pg_attribute AS a
              WHERE a.attrelid = to_regclass(t.tbl) AND a.attname = t.col
                AND NOT a.attisdropped AND a.attnum > 0
          )
        """,
        names,
        tables,
        columns,
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
        "api_surface": row["api_surface"],
        "target": row["target"],
        "schema_version": row["schema_version"],
        "protocol_profile": row["protocol_profile"],
        "phase": row["phase"],
        "pending_terminal_status": row["pending_terminal_status"],
        "validation_error": _load_json(row["validation_error"]),
        "input_file_id": row["input_file_id"],
        "output_file_id": row["output_file_id"],
        "error_file_id": row["error_file_id"],
        "results_expires_at": row["results_expires_at"],
        "finalizing_at": row["finalizing_at"],
        "sealed_at": row["sealed_at"],
        "cancelled_count": row["cancelled_count"],
        "expired_count": row["expired_count"],
        "prompt_tokens": int(row["prompt_tokens"] or 0),
        "completion_tokens": int(row["completion_tokens"] or 0),
        "unknown_cost_count": row["unknown_cost_count"],
        "idempotency_key": row["idempotency_key"],
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
        "usage": _load_json(row["usage"]),
        "cost_status": row["cost_status"],
        "attempts": row["attempts"],
        "model_id": row["model_id"],
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


__all__ = [
    "ACTIVE_BATCH_STATUSES",
    "SUPPORTED_JOB_KINDS",
    "TERMINAL_BATCH_STATUSES",
    "BatchDeleteInProgress",
    "BatchInputUnavailable",
    "BatchLimitExceeded",
    "BatchOwnerUnavailable",
    "BatchStore",
    "IdempotencyConflict",
    "IdempotencyTombstoned",
    "display_status_sql",
]
