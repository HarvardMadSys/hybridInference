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

The catalog check cannot help the deploy that actually adds a column, though —
that one has to take the lock. So the DDL phase additionally runs under a short
``lock_timeout`` and raises :class:`SchemaLockUnavailable` rather than waiting,
because a queued ``ACCESS EXCLUSIVE`` request blocks every reader behind it.
Callers are expected to retry it in the background instead of failing startup:
the usual lock holder is the nightly ``pg_dump``, which runs for hours.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    import asyncpg

# How long a migration waits for its table lock before Postgres cancels it.
#
# The catalog pre-check below removes the lock from the *steady-state* path, but
# not from the deploy that actually adds a column — that one has to take
# ``ACCESS EXCLUSIVE``. Waiting for it is the dangerous part: a queued exclusive
# request also parks every read that arrives behind it, so a migration blocked
# by one long-running reader stalls the whole table for as long as it waits.
# The readers that block it are long: the nightly ``pg_dump`` holds
# ``ACCESS SHARE`` over ``api_logs`` for hours. Bounded low so a contended
# migration fails immediately and leaves the queue clear; the caller retries it
# later rather than holding startup (or the table) hostage.
_DDL_LOCK_TIMEOUT = "3s"

# SQLSTATE Postgres raises when ``lock_timeout`` expires (asyncpg surfaces it as
# ``LockNotAvailableError``). Matched by code rather than by exception class so
# this module keeps its asyncpg import type-only.
_LOCK_NOT_AVAILABLE = "55P03"


class SchemaLockUnavailable(RuntimeError):
    """A migration could not acquire its table lock within the timeout.

    Distinct from a genuine schema error: the connection is healthy and the
    statement is valid, another session simply holds a conflicting lock. Callers
    should treat it as transient and retry, not as a reason to give up on the
    database.
    """


@asynccontextmanager
async def _bounded_lock_wait(conn: asyncpg.Connection) -> AsyncIterator[None]:
    """Bound how long DDL inside the block waits on a lock.

    Reset on the way out because the connection goes back to a pool: leaving
    ``lock_timeout`` set would silently apply it to ordinary query traffic.
    """
    await conn.execute(f"SET lock_timeout = '{_DDL_LOCK_TIMEOUT}'")
    try:
        yield
    finally:
        await conn.execute("SET lock_timeout = DEFAULT")


async def _execute_ddl(conn: asyncpg.Connection, ddl: str) -> None:
    """Run one DDL statement, translating a lock timeout into a typed error."""
    try:
        await conn.execute(ddl)
    except Exception as exc:
        if getattr(exc, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
            raise
        raise SchemaLockUnavailable(
            f"could not acquire the lock for {ddl!r} within {_DDL_LOCK_TIMEOUT}; "
            "another session holds a conflicting lock"
        ) from exc


# Public faces of the guard pair, for the other schema builders
# (``database.py`` / ``postgres_operational.py``) that adopt the same
# doctrine: catalog first, strong-lock DDL only when actually needed, and
# always under the bounded wait. The 2026-08-21 production outage was this
# module's api_logs incident replayed on ``api_keys`` — the boot-time
# builders still issued their idempotent ALTERs bare, and the first restart
# inside the nightly ``pg_dump`` window queued behind it for the pool's
# 60 s command timeout, three times, and failed the deploy.
bounded_ddl = _bounded_lock_wait
execute_ddl = _execute_ddl


async def existing_columns(conn: asyncpg.Connection, table: str) -> set[str]:
    """Column names of *table*; empty when the table does not exist."""
    rows = await conn.fetch(
        """
        SELECT attname
        FROM pg_attribute
        WHERE attrelid = to_regclass($1)
          AND NOT attisdropped
          AND attnum > 0
        """,
        table,
    )
    return {r["attname"] for r in rows}


async def apply_column_migrations(
    conn: asyncpg.Connection,
    table: str,
    migrations: Sequence[tuple[str, str]],
) -> None:
    """Issue only the ``ADD COLUMN`` DDL whose column is actually missing.

    The steady-state startup (every column already present) reads the catalog
    once and issues no DDL at all — no ``ACCESS EXCLUSIVE`` request, nothing
    for a long-running reader like the nightly ``pg_dump`` to block. DDL that
    does run sits under the bounded lock wait and surfaces contention as
    :class:`SchemaLockUnavailable`. The ``IF NOT EXISTS`` clause stays on the
    statements as the race guard for two workers booting at once.
    """
    present = await existing_columns(conn, table)
    pending = [ddl for name, ddl in migrations if name not in present]
    if not pending:
        return
    async with _bounded_lock_wait(conn):
        for ddl in pending:
            await _execute_ddl(conn, ddl)


async def drop_columns_if_present(
    conn: asyncpg.Connection,
    table: str,
    drops: Sequence[tuple[str, str]],
) -> None:
    """Issue only the ``DROP COLUMN`` DDL whose column still exists."""
    present = await existing_columns(conn, table)
    pending = [ddl for name, ddl in drops if name in present]
    if not pending:
        return
    async with _bounded_lock_wait(conn):
        for ddl in pending:
            await _execute_ddl(conn, ddl)


async def constraint_definition(conn: asyncpg.Connection, table: str, name: str) -> str | None:
    """``pg_get_constraintdef`` of *name* on *table*, or None when absent.

    Lets a constraint rebuild be gated on what the constraint currently says
    (e.g. skip when the CHECK already admits every required member) instead of
    unconditionally dropping and re-adding it — which takes ``ACCESS
    EXCLUSIVE`` on every single startup.
    """
    return await conn.fetchval(
        """
        SELECT pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = to_regclass($1)
          AND conname = $2
        """,
        table,
        name,
    )


def constraint_admitted_values(definition: str | None) -> set[str] | None:
    """The quoted string literals a constraint definition admits, or None.

    ``pg_get_constraintdef`` renders a CHECK's members as quoted literals
    (``role = ANY (ARRAY['free'::text, ...])``), so the set of quoted strings
    is the constraint's admitted vocabulary. A gate that skips a rebuild must
    compare this set **exactly**, never by membership: an older, wider
    constraint can admit every current member plus a legacy one —
    ``users_role_check`` once shipped as ``('trial', 'free', 'pro',
    'internal', 'admin')`` — and a subset test judges it settled, silently
    skipping both the rebuild and the row migration the rebuild carries.
    """
    if definition is None:
        return None
    return set(re.findall(r"'([^']*)'", definition))


async def column_metadata(
    conn: asyncpg.Connection, table: str, column: str
) -> dict[str, object] | None:
    """``{"not_null": bool, "default_expr": str | None}`` for one column.

    None when the table or column does not exist. Gates ``ALTER COLUMN ...
    SET DEFAULT / SET NOT NULL`` statements the same way the column
    migrations are gated: read the catalog, only lock when the change is
    actually needed.
    """
    row = await conn.fetchrow(
        """
        SELECT a.attnotnull AS not_null,
               pg_get_expr(d.adbin, d.adrelid) AS default_expr
        FROM pg_attribute a
        LEFT JOIN pg_attrdef d
          ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = to_regclass($1)
          AND a.attname = $2
          AND NOT a.attisdropped
        """,
        table,
        column,
    )
    return dict(row) if row is not None else None


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
            agent_job_id TEXT
        )
    """)

    # Snapshot the catalog once (cheap ACCESS SHARE) and only issue DDL for
    # objects that are missing, so the common already-migrated startup path
    # never queues an ACCESS EXCLUSIVE / SHARE lock on this hot table.
    existing_columns_now = await existing_columns(conn, "api_logs")
    existing_indexes = await _existing_indexes(conn)

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


# ---------------------------------------------------------------------------
# Erasure fence (issue #1421)
#
# A fire-and-forget log write can be queued *before* the hard-delete runs, or
# can BEGIN its INSERT before the purge and COMMIT after it. A second DELETE
# after the op_store wipe shrinks the window but does not close it — so we need
# a durable, Postgres-level fence that every api_logs writer must check under
# the same lock the hard-delete takes when establishing the tombstone.
#
# The fence table stores a non-reversible (HMAC-SHA256) digest of each erased
# account id. Storing a digest rather than the raw id means the fence does not
# reintroduce the raw account identity: given only
# ``erasure_fence.account_digest``, you cannot recover the original user_id
# without the secret key. The digest is computed with a dedicated, stable
# erasure-fence secret so that routine API_KEY_SECRET rotation does not
# invalidate existing fences (see :data:`ERASURE_FENCE_SETTING`).
#
# Concurrency protocol (per account):
#
#   LOG WRITE (one api_logs INSERT) — uses a SHARED lock so concurrent
#   writers for the same account do not serialize behind one another:
#       1. Compute fence_key = HMAC(secret, user_id || credential_owner_id).
#       2. BEGIN.
#       3. SELECT pg_advisory_xact_lock_shared(hashtext(fence_key)).
#          Concurrent shared-lock holders coexist; an exclusive hard-delete
#          blocks until they all COMMIT.
#       4. SELECT ... FROM erasure_fence WHERE account_digest = fence_key.
#       5. If a row is found: ROLLBACK (the identifying row must not exist).
#       6. Otherwise: INSERT INTO api_logs ...; COMMIT.
#
#   HARD DELETE — uses an EXCLUSIVE lock so it waits for all in-flight
#   writers and prevents new ones from crossing the fence:
#       1. BEGIN.
#       2. SELECT pg_advisory_xact_lock(hashtext(fence_key)).
#          This blocks until every in-flight shared-lock holder COMMITs.
#       3. INSERT INTO erasure_fence (account_digest) VALUES (fence_key)
#          ON CONFLICT (account_digest) DO NOTHING.
#       4. DELETE FROM api_logs WHERE user_id = $1
#          OR metadata->>'credential_owner_id' = $1.
#       5. COMMIT.
#
# Ordering proof:
#   - Shared locks coexist with shared locks; exclusive locks wait for all
#     shared holders and are waited on by subsequent shared holders.
#   - If writers hold shared locks first, the hard-delete waits for all of
#     them, then purges their rows. Any writer that started before the
#     hard-delete's exclusive lock either committed (and is purged) or is
#     blocked on the fence row it can now see.
#   - If the hard-delete holds the exclusive lock first, subsequent writers
#     wait for it to COMMIT, then see the fence row and abort.
#   - The hard-delete's INSERT and DELETE run in the same transaction, so
#     once it COMMITs, no identifying row can exist and no new one can be
#     inserted.
#
# Why advisory locks instead of SELECT ... FOR UPDATE:
#   ``SELECT ... FOR UPDATE`` only locks *existing* rows. When the fence row
#   does not yet exist, the SELECT returns empty and locks nothing — the
#   hard-delete could INSERT the fence and DELETE the rows while the log
#   write is paused between the check and the INSERT. Advisory locks
#   serialize on the key itself, regardless of whether the row exists.
#
# Why shared locks for writers:
#   An exclusive lock per writer would serialize all concurrent requests for
#   the same account — a severe hot-path regression. Shared locks let
#   ordinary writers proceed in parallel while still guaranteeing the
#   hard-delete waits for all of them.
#
# Why a trigger alone is insufficient: a BEFORE INSERT trigger cannot see the
# ``erasure_fence`` row being established by a concurrent uncommitted
# hard-delete transaction (READ COMMITTED visibility). The trigger would have
# to issue its own ``pg_advisory_xact_lock`` — which is exactly what the
# explicit protocol above does, and what a trigger would do implicitly anyway.
# We choose the explicit protocol because:
#   (a) it keeps the serialization visible in application code, not hidden in
#       trigger logic;
#   (b) it lets the log write ABORT cleanly (no "trigger raised exception"
#       error path that the fire-and-forget caller would have to swallow); and
#   (c) the same helper is used by both PostgresLogStore.log_request and
#       DatabaseLogger.log_request, so the two writers cannot drift.
# ---------------------------------------------------------------------------

#: Name of the erasure fence table.
ERASURE_FENCE_TABLE = "erasure_fence"

#: Column name of the digest primary key.
ERASURE_FENCE_DIGEST_COLUMN = "account_digest"


#: Setting key for the dedicated, stable erasure-fence derivation secret.
#: This is separate from API_KEY_SECRET so that routine API-key rotation
#: does not invalidate existing fences (which would allow a previously
#: erased account's logs to be written again — a privacy regression).
#: If unset, the gateway falls back to API_KEY_SECRET and logs a startup
#: warning; however, hard-delete will FAIL_CLOSED if no fence secret is
#: available at erasure time (see :func:`establish_erasure_fence`).
ERASURE_FENCE_SETTING = "ERASURE_FENCE_SECRET"


def fence_account_digest(user_id: str, secret: str) -> str:
    """Return the HMAC-SHA256 digest used as the fence key for *user_id*.

    Deterministic and non-reversible: given the digest and no secret you
    cannot recover the original account id.

    Args:
        user_id: the account identifier being erased.
        secret: a dedicated, stable erasure-fence secret (preferably
            :data`ERASURE_FENCE_SETTING`). Must be stable for the lifetime
            of any fence row — changing it fails startup fingerprint
            validation rather than safely rotating the existing namespace. Do
            NOT use a routinely-rotatable secret like
            API_KEY_SECRET unless you treat it as a stable erasure-fence secret.
            Once the fallback namespace is pinned, changing API_KEY_SECRET
            fails startup fingerprint validation; it does not safely rotate
            the existing fence namespace.
    """
    import hmac as _hmac

    mac = _hmac.new(secret.encode(), user_id.encode(), "sha256")
    return mac.hexdigest()


def fence_account_advisory_key(digest: str) -> int:
    """Derive a signed 64-bit Postgres advisory-lock key from a fence digest.

    Postgres advisory-lock functions take a single ``bigint`` key. We take
    the first 64 bits of the HMAC-SHA256 digest and interpret them as a
    signed big-endian int64. This preserves the full 256-bit collision
    resistance of the HMAC for all practical table sizes while giving
    Postgres a native integer key (no ``hashtext`` 32-bit reduction).
    """
    raw = bytes.fromhex(digest[:16])
    key = int.from_bytes(raw, byteorder="big", signed=True)
    return key


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
    # The PRIMARY KEY already creates the unique index; this is just a
    # comment-friendly anchor for future schema work. Kept separate from the
    # table DDL so the PK's implicit index is never accidentally dropped.


async def check_erasure_fence(
    conn: asyncpg.Connection,
    *,
    fence_keys: list[str],
) -> bool:
    """Return True if any *fence_key* currently has an active erasure tombstone.

    Takes a transaction-level SHARED advisory lock on each key so the caller
    can rely on the serialization described in the module docstring: a
    concurrent hard-delete establishing the same fence key holds an exclusive
    lock and blocks until all shared holders commit. If the hard-delete has
    already committed, its fence row is visible and we return True.

    Call this inside a transaction that subsequently either:
    - aborts (when this returns True — the row must not be written), or
    - inserts the api_logs row and commits (when this returns False).

    Args:
        conn: an asyncpg connection already inside a transaction.
        fence_keys: one or more HMAC digests (as returned by
            :func:`fence_account_digest`) to check. A log row may identify
            its account via ``user_id`` *or* ``metadata->>'credential_owner_id'``,
            so both digests are passed together.
    """
    if not fence_keys:
        return False
    # Sort keys to ensure consistent lock acquisition order and prevent
    # deadlocks between concurrent log writes checking overlapping key sets.
    fence_keys = sorted(set(fence_keys))
    # Take a transaction-level SHARED advisory lock on each key. Shared
    # locks coexist with other shared locks (concurrent writers for the
    # same account proceed in parallel) but block behind an exclusive
    # hard-delete lock and are waited on by subsequent exclusive lockers.
    for key in fence_keys:
        _adv_key = fence_account_advisory_key(key)
        await conn.execute("SELECT pg_advisory_xact_lock_shared($1)", _adv_key)
    row = await conn.fetchrow(
        f"""
        SELECT 1
        FROM {ERASURE_FENCE_TABLE}
        WHERE account_digest = ANY($1::text[])
        """,
        fence_keys,
    )
    return row is not None


async def protect_api_logs_insert(
    conn: asyncpg.Connection,
    *,
    fence_secret: str,
    user_id: str | None,
    credential_owner_id: str | None,
) -> bool:
    """Check the erasure fence before inserting an api_logs row.

    Determines the account identifiers this row could carry, takes a shared
    advisory lock on each, and checks for an active erasure tombstone. Returns
    True if the insert should be suppressed (fenced), False if it's safe to
    proceed.

    Call this inside the same transaction as the INSERT.

    Args:
        conn: an asyncpg connection already inside a transaction.
        fence_secret: the server's erasure-fence derivation secret.
        user_id: the ``user_id`` that will be stored in the row (or None).
        credential_owner_id: the ``metadata->>'credential_owner_id'`` that
            will be stored in the row (or None).
    """
    # A row may belong to both the authenticated user and the credential
    # owner. Every distinct identity must participate in the erasure fence;
    # fencing either identity suppresses the write.
    if not fence_secret or not (user_id or credential_owner_id):
        return False
    fence_keys = []
    if user_id:
        fence_keys.append(fence_account_digest(user_id, fence_secret))
    if credential_owner_id and credential_owner_id != user_id:
        fence_keys.append(fence_account_digest(credential_owner_id, fence_secret))
    return await check_erasure_fence(conn, fence_keys=fence_keys)


class ErasureFenceUnavailable(RuntimeError):
    """The erasure fence could not be established because no fence secret is
    available. Distinct from a transient lock timeout: the connection is
    healthy, but the deployment has not configured a stable erasure-fence
    secret and the fallback is also missing. Callers should treat this as
    a hard failure and abort the hard-delete rather than silently proceeding
    without the fence.
    """


def _non_blank_secret(value: str | None) -> str | None:
    """Return a normalized secret, or ``None`` when it is blank."""
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def resolve_fence_secret(secret: str | None) -> str:
    """Resolve the erasure-fence derivation secret.

    Prefers the dedicated ``ERASURE_FENCE_SECRET`` setting (or the
    *secret* argument if explicitly passed), falls back to
    ``API_KEY_SECRET`` if set, and raises :class:`ErasureFenceUnavailable`
    if neither is available.

    The canonical fence-secret source is the ``erasure_fence_secret``
    setting in ``serving.config.settings.Settings``. This is separate
    from ``API_KEY_SECRET`` so that routine API-key rotation does not
    invalidate existing fences. If the erasure-fence secret changes, restore
    the original secret instead; existing tombstones must remain intact
    because they are the durable deletion record.
    """

    # 1. Explicitly passed secret (for tests/diagnostics)
    if secret and secret.strip():
        return secret.strip()
    # 2. Dedicated setting (preferred for production)
    from serving.config.settings import get_settings

    settings = get_settings()
    dedicated_secret = _non_blank_secret(settings.erasure_fence_secret)
    if dedicated_secret is not None:
        return dedicated_secret
    # 3. Fallback to API_KEY_SECRET (with warning)
    import logging as _logging

    _log = _logging.getLogger(__name__)
    fallback_secret = _non_blank_secret(settings.api_key_secret)
    if fallback_secret is not None:
        _log.warning(
            "Erasure fence: using API_KEY_SECRET as the fence derivation "
            "secret (set erasure_fence_secret to a dedicated, stable secret "
            "to avoid this). Once pinned, changing API_KEY_SECRET causes "
            "startup fingerprint validation to fail; restore the pinned value "
            "instead of deleting fence data.",
        )
        return fallback_secret
    raise ErasureFenceUnavailable(
        "No erasure-fence secret is configured. Set erasure_fence_secret "
        "(or api_key_secret as a fallback) to enable the hard-delete "
        "erasure fence. Without it, the hard-delete endpoint cannot "
        "guarantee that fire-and-forget log writes will not recreate "
        "identifying rows."
    )


async def establish_erasure_fence(
    conn: asyncpg.Connection,
    *,
    fence_key: str,
) -> None:
    """Create the erasure tombstone for one account.

    Uses ``ON CONFLICT DO NOTHING`` so concurrent hard-delete calls for the
    same account are idempotent (no unique-violation error). A
    transaction-level EXCLUSIVE advisory lock on the key serializes against
    any log write holding a shared lock on the same key: the hard-delete
    blocks until all shared holders commit, then establishes the fence and
    purges.

    Args:
        conn: an asyncpg connection already inside a transaction.
        fence_key: the HMAC digest of the account being erased.
    """
    # Take the EXCLUSIVE advisory lock BEFORE inserting the fence row.
    # This blocks until every in-flight shared-lock holder commits, then
    # prevents new shared lockers from crossing the fence.
    _adv_key = fence_account_advisory_key(fence_key)
    await conn.execute("SELECT pg_advisory_xact_lock($1)", _adv_key)
    await conn.execute(
        f"""
        INSERT INTO {ERASURE_FENCE_TABLE} (account_digest)
        VALUES ($1)
        ON CONFLICT (account_digest) DO NOTHING
        """,
        fence_key,
    )


# ---------------------------------------------------------------------------
# Secret fingerprint pinning
# ---------------------------------------------------------------------------

_FINGERPRINT_CONTEXT = "hybridinference:erasure-fence:secret-fingerprint:v1"
_TABLE = "erasure_fence_metadata"
_KEY_COL = "config_key"
_VALUE_COL = "config_value"
_FINGERPRINT_KEY = "secret_fingerprint"
# A separate advisory-lock namespace serializes fingerprint initialization and
# the first tombstone established by a hard delete. The two-int form cannot
# collide with the one-bigint account locks used by the erasure protocol.
_FINGERPRINT_LOCK_CLASS = 0x45524153  # ASCII "ERAS"
_FINGERPRINT_LOCK_OBJECT = 1


def fingerprint_secret(secret: str) -> str:
    """Return a stable, domain-separated fingerprint of the fence secret."""
    import hashlib as _hashlib
    import hmac as _hmac

    return _hmac.new(_FINGERPRINT_CONTEXT.encode(), secret.encode(), _hashlib.sha256).hexdigest()


async def ensure_metadata_table(conn: asyncpg.Connection) -> None:
    """Create the singleton metadata table if it does not exist."""
    await conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            {_KEY_COL} TEXT PRIMARY KEY,
            {_VALUE_COL} TEXT NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)


async def get_pinned_fingerprint(conn: asyncpg.Connection) -> str | None:
    """Return the pinned secret fingerprint, or None if not yet set."""
    return await conn.fetchval(
        f"""
        SELECT {_VALUE_COL}
        FROM {_TABLE}
        WHERE {_KEY_COL} = $1
        """,
        _FINGERPRINT_KEY,
    )


async def try_insert_fingerprint(conn: asyncpg.Connection, *, fingerprint: str) -> bool:
    """Attempt to insert the fingerprint. Returns True if inserted, False if
    a fingerprint already existed (ON CONFLICT DO NOTHING)."""
    result = await conn.execute(
        f"""
        INSERT INTO {_TABLE} ({_KEY_COL}, {_VALUE_COL})
        VALUES ($1, $2)
        ON CONFLICT ({_KEY_COL}) DO NOTHING
        """,
        _FINGERPRINT_KEY,
        fingerprint,
    )
    # asyncpg returns 'INSERT 0 1' for a successful insert, 'INSERT 0 0' if
    # ON CONFLICT DO NOTHING skipped.
    return result.endswith("1")


async def _validate_or_init_fingerprint_in_transaction(
    conn: asyncpg.Connection,
    *,
    secret: str,
) -> None:
    await ensure_erasure_fence_table(conn)
    await ensure_metadata_table(conn)
    current = fingerprint_secret(secret)

    # Serialize the empty-fence transition with the hard-delete path. Without
    # this lock, a startup could repin metadata while a first tombstone is
    # being committed under a different secret.
    await conn.execute(
        "SELECT pg_advisory_xact_lock($1, $2)",
        _FINGERPRINT_LOCK_CLASS,
        _FINGERPRINT_LOCK_OBJECT,
    )
    stored = await get_pinned_fingerprint(conn)
    # Pin the namespace before the process starts serving. This must happen
    # even when the fence table is empty: otherwise two workers can validate
    # different secrets before the first deletion, then use incompatible
    # digest namespaces after that deletion commits.
    if stored is None:
        inserted = await try_insert_fingerprint(conn, fingerprint=current)
        if inserted:
            return
        stored = await get_pinned_fingerprint(conn)
    if stored != current:
        raise ErasureFenceUnavailable(
            "The configured erasure-fence secret does not match the "
            "pinned fingerprint. The secret was likely rotated after "
            "fence rows were created. Restore the original "
            "ERASURE_FENCE_SECRET before starting this process; do not "
            "delete erasure_fence tombstones or fingerprint metadata."
        )


async def validate_or_init_fingerprint(
    conn: asyncpg.Connection,
    *,
    secret: str,
) -> None:
    """Validate the configured secret while holding the fingerprint lock.

    The first successful initializer pins the derivation namespace before the
    process can serve requests. Every subsequent initializer must match it,
    including when no erasure tombstone exists yet. This prevents workers
    starting with different secrets from passing startup and later using
    incompatible fence namespaces.

    The validation sequence owns a transaction when the caller has not
    already opened one. Callers such as hard-delete that already hold the
    transaction keep that outer transaction, so the advisory lock remains
    held through the fence write and purge.

    Raises :class:`ErasureFenceUnavailable` on mismatch.
    """
    if conn.is_in_transaction():
        await _validate_or_init_fingerprint_in_transaction(conn, secret=secret)
        return
    async with conn.transaction():
        await _validate_or_init_fingerprint_in_transaction(conn, secret=secret)
