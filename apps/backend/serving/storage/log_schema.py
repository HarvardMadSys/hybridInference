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
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

# Columns added over the table's lifetime. Applied as idempotent ``ADD COLUMN IF
# NOT EXISTS`` migrations so databases created by an earlier revision are
# backfilled. Kept in sync with the ``CREATE TABLE`` below, which lists the same
# columns so fresh installs start complete (the migrations are then no-ops).
_API_LOGS_COLUMN_MIGRATIONS = [
    "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS reasoning_tokens INTEGER",
    "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS stream BOOLEAN",
    "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS ttft_ms INTEGER",
    "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER",
    "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER",
    "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS cost_usd DECIMAL(12, 8)",
    "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS upstream_cost_usd DECIMAL(12, 8)",
    "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS request_payload JSONB",
    "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS num_turns INTEGER",
    "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS num_user_turns INTEGER",
    "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS num_tool_calls INTEGER",
    "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS last_user_msg_chars INTEGER",
    "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS last_user_msg_entropy REAL",
    "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS last_user_msg_hash BIGINT",
    # Which model/endpoint actually SERVED the request, promoted from the routing
    # metadata into queryable columns (the model the request resolved to after
    # aliasing/rerouting, and the specific endpoint among the route's
    # candidates). model_id remains the client-requested model. Feeds
    # smart-router training queries.
    "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS served_model_id TEXT",
    "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS served_endpoint_id TEXT",
]

# Created after the column migrations so a predicate / key column referencing a
# migrated column (e.g. cost_usd, served_endpoint_id) always exists first —
# otherwise ``CREATE INDEX`` raises ``UndefinedColumnError`` and aborts init.
_API_LOGS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_api_logs_timestamp ON api_logs(timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_api_logs_model ON api_logs(model_id, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_api_logs_provider ON api_logs(provider, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_api_logs_request_id ON api_logs(request_id)",
    "CREATE INDEX IF NOT EXISTS idx_api_logs_user "
    "ON api_logs(user_id, timestamp DESC) WHERE user_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_api_logs_session "
    "ON api_logs(session_id, timestamp DESC) WHERE session_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_api_logs_model_activity "
    "ON api_logs(timestamp DESC, model_id, provider) WHERE user_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_api_logs_error "
    "ON api_logs(timestamp DESC) WHERE error IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_api_logs_user_cost ON api_logs(user_id, timestamp, cost_usd)",
    "CREATE INDEX IF NOT EXISTS idx_api_logs_served_endpoint "
    "ON api_logs(served_endpoint_id, timestamp DESC) WHERE served_endpoint_id IS NOT NULL",
]


async def ensure_api_logs_schema(conn: asyncpg.Connection) -> None:
    """Create and migrate ``api_logs`` + ``api_stats_hourly`` on *conn*.

    Idempotent: safe on a fresh database (the ``CREATE TABLE`` carries the full
    current column set) and on an existing one (the ``ADD COLUMN IF NOT EXISTS``
    migrations backfill columns added over time). Indexes are always created
    after the column migrations so a predicate/key column can never reference a
    not-yet-added column.
    """
    # Fresh installs get the complete, current schema up front; the column
    # migrations below only matter when upgrading a database created by an
    # earlier revision (they are no-ops on a fresh table).
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

    # Drop legacy columns/indexes from very old databases.
    await conn.execute("DROP INDEX IF EXISTS idx_api_logs_prompt_hash")
    await conn.execute("DROP INDEX IF EXISTS idx_api_logs_response_hash")
    await conn.execute("ALTER TABLE api_logs DROP COLUMN IF EXISTS prompt_hash")
    await conn.execute("ALTER TABLE api_logs DROP COLUMN IF EXISTS response_hash")

    for col_ddl in _API_LOGS_COLUMN_MIGRATIONS:
        await conn.execute(col_ddl)

    for index_ddl in _API_LOGS_INDEXES:
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
