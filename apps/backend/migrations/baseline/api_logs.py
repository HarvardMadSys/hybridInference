"""``api_logs``, its hourly and daily rollups, and the erasure-fence tables, frozen.

Copied from ``serving.storage.log_schema``, which both startup builders shared.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from migrations.baseline.ddl import (
    bounded_ddl as _bounded_lock_wait,
    execute_ddl as _execute_ddl,
    existing_columns,
)

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
    # Cloud agent sandbox (issue #1041): the agent job whose sandbox issued
    # this request, so a job's model spend can be summed from the same ledger
    # that bills everything else — no need to trust an agent's self-reported
    # usage. NULL for all ordinary traffic.
    ("agent_job_id", "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS agent_job_id TEXT"),
    # Batch inference (``/v1/batches``): the batch whose worker issued this
    # request on its owner's behalf, so a batch's model spend can be read from
    # the same ledger as everything else. NULL for all other traffic.
    # Deliberately not indexed: no request path reads api_logs by batch (the
    # batch keeps its own counts and cost), and an index built here would hold
    # a SHARE lock blocking every log write for a full scan of the table.
    ("batch_job_id", "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS batch_job_id TEXT"),
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
        "idx_api_logs_agent_job",
        # Partial: agent traffic is a tiny slice of the table, and the query
        # that matters (sum this job's spend, on every model call the sandbox
        # makes) is keyed purely on job id.
        "CREATE INDEX IF NOT EXISTS idx_api_logs_agent_job "
        "ON api_logs(agent_job_id) WHERE agent_job_id IS NOT NULL",
    ),
    (
        "idx_api_logs_credential_owner",
        # Partial, and for one query: the admin hard-delete has to reach rows
        # that name an account in metadata rather than in user_id (a caller the
        # gateway identified without authenticating — see
        # ``observability/rejection_log``). Those rows are a tiny slice of the
        # table, so the index is small; without it that purge degrades from an
        # indexed delete to a scan of the whole table. The predicate is
        # ``IS NOT NULL`` rather than a ``metadata ? key`` test so the planner
        # can prove an equality lookup implies it and actually use the index.
        "CREATE INDEX IF NOT EXISTS idx_api_logs_credential_owner "
        "ON api_logs ((metadata->>'credential_owner_id')) "
        "WHERE metadata->>'credential_owner_id' IS NOT NULL",
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

# Completed-hour counts behind the admin Request volume cards, written and read
# by ``serving.servers.routers.admin.request_volume``. One row per UTC hour,
# quiet hours included, so a row's presence means "this hour is counted".
_REQUEST_VOLUME_HOURLY_DDL = """
    CREATE TABLE IF NOT EXISTS request_volume_hourly (
        hour_bucket             TIMESTAMPTZ PRIMARY KEY,
        request_count           BIGINT      NOT NULL,
        success_count           BIGINT      NOT NULL,
        error_count             BIGINT      NOT NULL,
        client_disconnect_count BIGINT      NOT NULL,
        latency_count           BIGINT      NOT NULL,
        latency_sum_ms          BIGINT      NOT NULL,
        completed_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
"""

# Completed-day request and token totals per user behind the admin Users table,
# written and read by ``serving.storage.user_usage_rollup``. A user with no
# traffic on a day has no row, so the coverage table holds one row per counted
# UTC day, quiet days included: its presence, with the current ``definition``,
# means "this day is counted".
_USER_USAGE_DAILY_DDL = (
    """
    CREATE TABLE IF NOT EXISTS user_usage_daily (
        user_id            TEXT   NOT NULL,
        day                DATE   NOT NULL,
        request_count      BIGINT NOT NULL,
        total_tokens       BIGINT NOT NULL,
        turns_sum          BIGINT NOT NULL DEFAULT 0,
        turns_count        BIGINT NOT NULL DEFAULT 0,
        user_turns_sum     BIGINT NOT NULL DEFAULT 0,
        user_turns_count   BIGINT NOT NULL DEFAULT 0,
        early_requests     BIGINT NOT NULL DEFAULT 0,
        early_ask_requests BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, day)
    )
""",
    """
    CREATE TABLE IF NOT EXISTS user_usage_daily_coverage (
        day          DATE        PRIMARY KEY,
        completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        definition   TEXT        NOT NULL DEFAULT ''
    )
""",
)

# One row per stored UTC day behind the admin Analytics growth series, written
# and read by ``serving.storage.daily_activity_rollup``: the day's requests,
# prompt + completion tokens, and the sorted ids of its signed-in users.
_DAILY_ACTIVITY_DDL = """
    CREATE TABLE IF NOT EXISTS daily_activity (
        day          DATE        PRIMARY KEY,
        requests     BIGINT      NOT NULL,
        tokens       BIGINT      NOT NULL,
        user_ids     TEXT[]      NOT NULL,
        definition   TEXT        NOT NULL,
        completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
"""

# The last computed result of each slow admin view over api_logs -- the Providers
# tab's performance distributions and TTFT scatter -- as served, so a restarted
# gateway serves it at once instead of recomputing first. Written and read by
# ``serving.storage.admin_view_results``; ``definition`` fingerprints the
# computation, so a result an older revision computed is not served, and
# ``admin_view_state``'s one row holds the generation a user's hard delete
# advances, so a result computed before it is never published after it.
_ADMIN_VIEW_RESULTS_DDL = (
    """
    CREATE TABLE IF NOT EXISTS admin_view_results (
        name        TEXT        PRIMARY KEY,
        definition  TEXT        NOT NULL,
        payload     JSONB       NOT NULL,
        computed_at TIMESTAMPTZ NOT NULL,
        generation  BIGINT      NOT NULL
    )
""",
    """
    CREATE TABLE IF NOT EXISTS admin_view_state (
        id         BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
        generation BIGINT  NOT NULL
    )
""",
    # The row must exist before any write locks it: a write that found none
    # would have nothing to wait on while a delete created it.
    "INSERT INTO admin_view_state (id, generation) VALUES (TRUE, 0) ON CONFLICT (id) DO NOTHING",
)

# Storage parameters api_logs carries, as ``(name, value)``, set when absent (a
# value an operator set by hand is left alone).
#
# Insert-triggered autovacuum by default waits for inserts of 20% of the table:
# on a ten-million-row api_logs, ten days of traffic. Until a page is vacuumed
# the visibility map does not mark it all-visible, and an index-only scan --
# the admin performance distributions' (``serving.storage.perf_metrics_index``)
# -- reads the heap for it after all. 1% keeps that to about half a day. Such a
# vacuum visits only the pages written since the last, and skips index cleanup
# when there is nothing dead to remove.
_API_LOGS_STORAGE_PARAMETERS = (("autovacuum_vacuum_insert_scale_factor", "0.01"),)


# Columns the tables gained after they first shipped, as ``(table, column, ddl)``.
# A constant default makes each ``ADD COLUMN`` a catalog-only change on these
# small tables. Days stored before keep the default ``definition`` and so no
# longer count as stored: they are counted live and filled again with every
# column (see ``serving.storage.user_usage_rollup.DEFINITION``).
_USER_USAGE_DAILY_MIGRATIONS = [
    *(
        (
            "user_usage_daily",
            column,
            f"ALTER TABLE user_usage_daily ADD COLUMN IF NOT EXISTS {column} BIGINT NOT NULL DEFAULT 0",
        )
        for column in (
            "turns_sum",
            "turns_count",
            "user_turns_sum",
            "user_turns_count",
            "early_requests",
            "early_ask_requests",
        )
    ),
    (
        "user_usage_daily_coverage",
        "definition",
        "ALTER TABLE user_usage_daily_coverage "
        "ADD COLUMN IF NOT EXISTS definition TEXT NOT NULL DEFAULT ''",
    ),
]


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


async def _storage_parameters(conn: asyncpg.Connection) -> set[str]:
    """Return the names of the storage parameters set on ``api_logs``."""
    rows = await conn.fetch(
        """
        SELECT split_part(option, '=', 1) AS name
        FROM pg_class c, unnest(c.reloptions) AS option
        WHERE c.relname = 'api_logs' AND c.relkind = 'r'
          AND c.relnamespace = ANY(
              SELECT oid FROM pg_namespace WHERE nspname = ANY(current_schemas(false))
          )
        """
    )
    return {r["name"] for r in rows}


async def ensure_api_logs_schema(conn: asyncpg.Connection) -> None:
    """Create and migrate ``api_logs`` and its aggregate tables on *conn*.

    Idempotent: safe on a fresh database (the ``CREATE TABLE`` carries the full
    current column set) and on an existing one (missing columns/indexes are
    backfilled). Each migration/index is checked against the system catalogs
    first and only issued when actually missing, so a steady-state startup takes
    no strong table locks. Indexes are always created after the column
    migrations so a predicate/key column can never reference a not-yet-added
    column.
    """
    # These tables are runtime prerequisites for every identifying api_logs
    # write and hard-delete. Create them before entering the deferrable
    # api_logs migration sequence so a bounded lock timeout cannot leave a
    # usable pool without its privacy fence.
    await ensure_erasure_fence_table(conn)
    await ensure_metadata_table(conn)

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
            served_endpoint_id TEXT,
            agent_job_id TEXT,
            batch_job_id TEXT
        )
    """)

    # New, separate tables: creating them locks nothing the log writers use, so
    # they go before the deferrable sequence below rather than waiting on it.
    await conn.execute(_REQUEST_VOLUME_HOURLY_DDL)
    for ddl in _USER_USAGE_DAILY_DDL:
        await conn.execute(ddl)
    await conn.execute(_DAILY_ACTIVITY_DDL)
    for ddl in _ADMIN_VIEW_RESULTS_DDL:
        await conn.execute(ddl)

    # Snapshot the catalog once (cheap ACCESS SHARE) and only issue DDL for
    # objects that are missing, so the common already-migrated startup path
    # never queues an ACCESS EXCLUSIVE / SHARE lock on this hot table.
    existing_columns_now = await existing_columns(conn, "api_logs")
    existing_indexes = await _existing_indexes(conn)
    usage_columns = {
        table: await existing_columns(conn, table)
        for table in ("user_usage_daily", "user_usage_daily_coverage")
    }
    storage_parameters = await _storage_parameters(conn)

    # Everything below takes a strong lock, so it runs under a bounded wait: if
    # a concurrent reader holds the table, fail fast (SchemaLockUnavailable)
    # instead of queueing an exclusive request that blocks the table behind it.
    async with _bounded_lock_wait(conn):
        # Drop legacy columns/indexes from very old databases.
        for index_name, drop_ddl in _API_LOGS_LEGACY_INDEX_DROPS:
            if index_name in existing_indexes:
                await _execute_ddl(conn, drop_ddl)
        for col_name, drop_ddl in _API_LOGS_LEGACY_COLUMN_DROPS:
            if col_name in existing_columns_now:
                await _execute_ddl(conn, drop_ddl)

        for col_name, col_ddl in _API_LOGS_COLUMN_MIGRATIONS:
            if col_name not in existing_columns_now:
                await _execute_ddl(conn, col_ddl)

        for index_name, index_ddl in _API_LOGS_INDEXES:
            if index_name not in existing_indexes:
                await _execute_ddl(conn, index_ddl)

        for name, value in _API_LOGS_STORAGE_PARAMETERS:
            if name not in storage_parameters:
                await _execute_ddl(conn, f"ALTER TABLE api_logs SET ({name} = {value})")

        # Small tables, but the nightly pg_dump holds them as long as api_logs.
        for table, column, ddl in _USER_USAGE_DAILY_MIGRATIONS:
            if column not in usage_columns[table]:
                await _execute_ddl(conn, ddl)

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


async def ensure_erasure_fence_table(conn: asyncpg.Connection) -> None:
    """Create the ``erasure_fence`` table and its index if they are missing.

    Idempotent. The table is small (one row per hard-deleted account) and the
    primary-key index is the only access path either side needs:
    ``SELECT ... FOR UPDATE`` (log writer) and ``INSERT ... ON CONFLICT``
    (hard-delete) both resolve to the same digest, so both are single-row
    index lookups.

    A fence row stores only the non-reversible HMAC digest of the erased
    account id — never the raw id itself. This keeps the fence from
    reintroducing the identity the hard-delete is meant to purge: the
    digest cannot be reversed to the original id without the server secret.
    """
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS erasure_fence (
            account_digest TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)


_TABLE = "erasure_fence_metadata"
_KEY_COL = "config_key"
_VALUE_COL = "config_value"


async def ensure_metadata_table(conn: asyncpg.Connection) -> None:
    """Create the singleton metadata table if it does not exist."""
    await conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            {_KEY_COL} TEXT PRIMARY KEY,
            {_VALUE_COL} TEXT NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
