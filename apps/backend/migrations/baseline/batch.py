"""The batch tables, their later columns and their indexes, frozen.

Copied from ``serving.storage.batch_store``.
"""

from __future__ import annotations

from typing import Any

from migrations.baseline.ddl import bounded_ddl, execute_ddl, existing_columns

# Serializes schema creation across processes booting at once.
_SCHEMA_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('hybridinference:batch-schema'))"


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
            # Batches created in the console (``created_via = 'console'``): the
            # name and description the owner gave, and how it was configured
            # (source file, overrides, confirmed model, credential revision,
            # request digest). NULL for every batch the API created.
            ("display_name", "TEXT"),
            ("description", "TEXT"),
            ("created_via", "TEXT"),
            ("console_spec", "JSONB"),
            # Why the batch is paused (``quota`` or ``role``); NULL from a
            # worker of an earlier version, which only sets ``paused_until``.
            ("paused_reason", "TEXT"),
        ),
    ),
    (
        # The stored-request copy a console batch makes of its source file
        # (``derived_from_file_id``), owned by the batch that adopted it.
        "batch_files",
        (
            ("derived_from_file_id", "TEXT"),
            ("adopted_by_batch_id", "TEXT"),
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


async def create_tables(conn: Any) -> None:
    """Create the batch tables, their new columns and indexes (idempotent)."""
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
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}",
                    )
        for name, ddl in _INDEXES:
            if name in missing:
                await execute_ddl(conn, ddl)
