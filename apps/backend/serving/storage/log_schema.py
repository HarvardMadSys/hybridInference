"""Single source of truth for the ``api_logs`` / ``api_stats_hourly`` schema.

Two code paths build these tables: ``DatabaseLogger._create_tables`` (run at
bootstrap, ``database.py``) and ``PostgresLogStore.initialize`` (the runtime log
store, ``postgres_log.py``). Previously each carried its own hand-maintained copy
of the DDL. That duplication drifted and caused a production outage: a new column
pair (``served_model_id`` / ``served_endpoint_id``) was added to
``PostgresLogStore`` only, but the boot-time ``DatabaseLogger`` builder is the one
that actually runs — so the columns were never created and every insert failed
with ``UndefinedColumnError``.

To make that class of bug impossible, all ``api_logs`` schema — the table, its
migrations, its indexes, and the aggregated ``api_stats_hourly`` table — lives
here and is applied by both callers. Add future columns/indexes in this module
only.

Lock safety: ``ALTER TABLE`` / ``CREATE INDEX`` acquire strong locks
(``ACCESS EXCLUSIVE`` / ``SHARE``) *before* Postgres evaluates their
``IF [NOT] EXISTS`` clause, so blindly issuing every migration on each startup
would repeatedly queue restrictive locks on a hot table — a request that queues
behind a long-running query blocks every query behind it. This initializer runs
while other workers may be serving traffic (rolling deploys, container restarts,
worker scaling), so it first reads the catalogs (``pg_attribute`` / ``pg_indexes``,
cheap ``ACCESS SHARE``) and only issues the DDL that is actually missing. The
``IF [NOT] EXISTS`` clauses are kept on the statements that do run so two workers
booting at once can't collide on the create.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

# ``(name, ddl)`` for each column added over the table's lifetime, applied as
# idempotent ``ADD COLUMN IF NOT EXISTS`` migrations so databases created by an
# earlier revision are backfilled. ``name`` lets us skip the statement (and its
# lock) when the column already exists. Kept in sync with the ``CREATE TABLE``
# below, which lists the same columns so fresh installs start complete.
_API_LOGS_COLUMN_MIGRATIONS = [
    ("reasoning_tokens", "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS reasoning_tokens INTEGER"),
    ("stream", "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS stream BOOLEAN"),
    ("ttft_ms", "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS ttft_ms INTEGER"),
    (
        "cache_read_tokens",
        "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER",
    ),
    (
        "cache_write_tokens",
        "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER",
    ),
    ("cost_usd", "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS cost_usd DECIMAL(12, 8)"),
    (
        "upstream_cost_usd",
        "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS upstream_cost_usd DECIMAL(12, 8)",
    ),
    ("request_payload", "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS request_payload JSONB"),
    ("num_turns", "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS num_turns INTEGER"),
    ("num_user_turns", "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS num_user_turns INTEGER"),
    ("num_tool_calls", "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS num_tool_calls INTEGER"),
    (
        "last_user_msg_chars",
        "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS last_user_msg_chars INTEGER",
    ),
    (
        "last_user_msg_entropy",
        "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS last_user_msg_entropy REAL",
    ),
    (
        "last_user_msg_hash",
        "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS last_user_msg_hash BIGINT",
    ),
    # Which model/endpoint actually SERVED the request, promoted from the routing
    # metadata into queryable columns (the model the request resolved to after
    # aliasing/rerouting, and the specific endpoint among the route's
    # candidates). model_id remains the client-requested model. Feeds
    # smart-router training queries.
    ("served_model_id", "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS served_model_id TEXT"),
    (
        "served_endpoint_id",
        "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS served_endpoint_id TEXT",
    ),
]

# ``(name, ddl)`` per index. Created after the column migrations so a predicate /
# key column referencing a migrated column (e.g. cost_usd, served_endpoint_id)
# always exists first — otherwise ``CREATE INDEX`` raises ``UndefinedColumnError``
# and aborts init. ``name`` lets us skip the statement (and its lock) when the
# index already exists.
_API_LOGS_INDEXES = [
    (
        "idx_api_logs_timestamp",
        "CREATE INDEX IF NOT EXISTS idx_api_logs_timestamp ON api_logs(timestamp DESC)",
    ),
    (
        "idx_api_logs_model",
        "CREATE INDEX IF NOT EXISTS idx_api_logs_model ON api_logs(model_id, timestamp DESC)",
    ),
    (
        "idx_api_logs_provider",
        "CREATE INDEX IF NOT EXISTS idx_api_logs_provider ON api_logs(provider, timestamp DESC)",
    ),
    (
        "idx_api_logs_request_id",
        "CREATE INDEX IF NOT EXISTS idx_api_logs_request_id ON api_logs(request_id)",
    ),
    (
        "idx_api_logs_user",
        "CREATE INDEX IF NOT EXISTS idx_api_logs_user "
        "ON api_logs(user_id, timestamp DESC) WHERE user_id IS NOT NULL",
    ),
    (
        "idx_api_logs_session",
        "CREATE INDEX IF NOT EXISTS idx_api_logs_session "
        "ON api_logs(session_id, timestamp DESC) WHERE session_id IS NOT NULL",
    ),
    (
        "idx_api_logs_model_activity",
        "CREATE INDEX IF NOT EXISTS idx_api_logs_model_activity "
        "ON api_logs(timestamp DESC, model_id, provider) WHERE user_id IS NOT NULL",
    ),
    (
        "idx_api_logs_error",
        "CREATE INDEX IF NOT EXISTS idx_api_logs_error "
        "ON api_logs(timestamp DESC) WHERE error IS NOT NULL",
    ),
    (
        "idx_api_logs_user_cost",
        "CREATE INDEX IF NOT EXISTS idx_api_logs_user_cost ON api_logs(user_id, timestamp, cost_usd)",
    ),
    (
        "idx_api_logs_served_endpoint",
        "CREATE INDEX IF NOT EXISTS idx_api_logs_served_endpoint "
        "ON api_logs(served_endpoint_id, timestamp DESC) WHERE served_endpoint_id IS NOT NULL",
    ),
]

# ``(name, ddl)`` for legacy columns/indexes dropped from very old databases.
# Guarded by the catalog snapshot so the drop (and its ACCESS EXCLUSIVE lock)
# only runs when the object is actually still present.
_API_LOGS_LEGACY_INDEX_DROPS = [
    ("idx_api_logs_prompt_hash", "DROP INDEX IF EXISTS idx_api_logs_prompt_hash"),
    ("idx_api_logs_response_hash", "DROP INDEX IF EXISTS idx_api_logs_response_hash"),
]
_API_LOGS_LEGACY_COLUMN_DROPS = [
    ("prompt_hash", "ALTER TABLE api_logs DROP COLUMN IF EXISTS prompt_hash"),
    ("response_hash", "ALTER TABLE api_logs DROP COLUMN IF EXISTS response_hash"),
]


async def _existing_columns(conn: asyncpg.Connection) -> set[str]:
    """Return the live (non-dropped) column names of ``api_logs``."""
    rows = await conn.fetch(
        """
        SELECT attname
        FROM pg_attribute
        WHERE attrelid = 'api_logs'::regclass
          AND NOT attisdropped
          AND attnum > 0
        """
    )
    return {r["attname"] for r in rows}


async def _existing_indexes(conn: asyncpg.Connection) -> set[str]:
    """Return the index names defined on ``api_logs`` in the search path."""
    rows = await conn.fetch(
        """
        SELECT indexname
        FROM pg_indexes
        WHERE tablename = 'api_logs'
          AND schemaname = ANY(current_schemas(false))
        """
    )
    return {r["indexname"] for r in rows}


async def ensure_api_logs_schema(conn: asyncpg.Connection) -> None:
    """Create and migrate ``api_logs`` + ``api_stats_hourly`` on *conn*.

    Idempotent: safe on a fresh database (the ``CREATE TABLE`` carries the full
    current column set) and on an existing one (missing columns/indexes are
    backfilled). Each migration/index is checked against the system catalogs
    first and only issued when actually missing, so a steady-state startup takes
    no strong table locks. Indexes are always created after the column
    migrations so a predicate/key column can never reference a not-yet-added
    column.
    """
    # Fresh installs get the complete, current schema up front; the column
    # migrations below only matter when upgrading a database created by an
    # earlier revision (they are skipped on a fresh table).
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS api_logs (
            id BIGSERIAL PRIMARY KEY,
            timestamp TIMESTAMPTZ DEFAULT NOW(),
            request_id TEXT NOT NULL UNIQUE,
            model_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            temperature FLOAT,
            top_p FLOAT,
            max_tokens INTEGER,
            seed INTEGER,
            stream BOOLEAN,
            ttft_ms INTEGER,
            latency_ms INTEGER,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            reasoning_tokens INTEGER,
            total_tokens INTEGER,
            prompt TEXT,
            response TEXT,
            request_payload JSONB,
            status_code INTEGER,
            error TEXT,
            user_id TEXT,
            session_id TEXT,
            metadata JSONB,
            tools JSONB,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            cost_usd DECIMAL(12, 8),
            upstream_cost_usd DECIMAL(12, 8),
            num_turns INTEGER,
            num_user_turns INTEGER,
            num_tool_calls INTEGER,
            last_user_msg_chars INTEGER,
            last_user_msg_entropy REAL,
            last_user_msg_hash BIGINT,
            served_model_id TEXT,
            served_endpoint_id TEXT
        )
    """)

    # Snapshot the catalog once (cheap ACCESS SHARE) and only issue DDL for
    # objects that are missing, so the common already-migrated startup path
    # never queues an ACCESS EXCLUSIVE / SHARE lock on this hot table.
    existing_columns = await _existing_columns(conn)
    existing_indexes = await _existing_indexes(conn)

    # Drop legacy columns/indexes from very old databases.
    for index_name, drop_ddl in _API_LOGS_LEGACY_INDEX_DROPS:
        if index_name in existing_indexes:
            await conn.execute(drop_ddl)
    for col_name, drop_ddl in _API_LOGS_LEGACY_COLUMN_DROPS:
        if col_name in existing_columns:
            await conn.execute(drop_ddl)

    for col_name, col_ddl in _API_LOGS_COLUMN_MIGRATIONS:
        if col_name not in existing_columns:
            await conn.execute(col_ddl)

    for index_name, index_ddl in _API_LOGS_INDEXES:
        if index_name not in existing_indexes:
            await conn.execute(index_ddl)

    # Aggregated hourly stats.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS api_stats_hourly (
            hour TIMESTAMPTZ NOT NULL,
            model_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            request_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0,
            total_prompt_tokens BIGINT DEFAULT 0,
            total_completion_tokens BIGINT DEFAULT 0,
            total_tokens BIGINT DEFAULT 0,
            avg_latency_ms FLOAT,
            p50_latency_ms INTEGER,
            p95_latency_ms INTEGER,
            p99_latency_ms INTEGER,
            PRIMARY KEY (hour, model_id, provider)
        )
    """)
