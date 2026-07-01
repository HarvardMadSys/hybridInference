"""Schema-init guards for the shared ``api_logs`` schema.

The ``api_logs`` schema is built by two callers — ``DatabaseLogger._create_tables``
(bootstrap) and ``PostgresLogStore.initialize`` (runtime store) — that both
delegate to ``serving.storage.log_schema.ensure_api_logs_schema``. These tests
lock in the invariants that a past outage violated:

1. ``served_model_id`` / ``served_endpoint_id`` are added (and present in the
   ``CREATE TABLE`` so fresh installs start complete).
2. Any index referencing ``served_endpoint_id`` is created *after* the column
   exists — otherwise ``CREATE INDEX`` raises ``UndefinedColumnError`` and aborts
   initialization.
3. Both callers emit an identical ``api_logs`` DDL sequence, so the two
   initializers cannot drift (the root cause of the original outage).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.database import DatabaseLogger
from serving.storage.log_schema import (
    _API_LOGS_COLUMN_MIGRATIONS,
    _API_LOGS_INDEXES,
    ensure_api_logs_schema,
)
from serving.storage.postgres_log import PostgresLogStore


def _capturing_conn() -> tuple[MagicMock, list[str]]:
    """Return ``(conn, statements)`` recording every SQL string executed.

    ``conn.fetch`` (the catalog snapshot the schema helper reads before issuing
    DDL) defaults to empty, i.e. "nothing exists yet", so every migration/index
    is emitted — the fresh-install path.
    """
    statements: list[str] = []
    conn = MagicMock()

    async def _execute(sql: str, *_args: object) -> str:
        statements.append(sql)
        return "OK"

    conn.execute = AsyncMock(side_effect=_execute)
    conn.fetch = AsyncMock(return_value=[])
    return conn, statements


def _capturing_pool() -> tuple[MagicMock, list[str]]:
    """Return ``(pool, statements)`` whose acquired conn records executed SQL."""
    conn, statements = _capturing_conn()
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire_cm
    return pool, statements


def _api_logs_statements(statements: list[str]) -> list[str]:
    """Keep only the statements touching the shared log tables."""
    return [s for s in statements if "api_logs" in s or "api_stats_hourly" in s]


def _assert_served_columns_before_index(statements: list[str]) -> None:
    joined = "\n".join(statements)
    assert "served_model_id" in joined, "served_model_id column is never added"
    assert "served_endpoint_id" in joined, "served_endpoint_id column is never added"

    add_idx = next(
        i for i, s in enumerate(statements) if "ADD COLUMN" in s and "served_endpoint_id" in s
    )
    index_stmts = [
        i for i, s in enumerate(statements) if "CREATE INDEX" in s and "served_endpoint_id" in s
    ]
    for index_idx in index_stmts:
        assert add_idx < index_idx, (
            "an index references served_endpoint_id before the column is added; "
            "CREATE INDEX would raise UndefinedColumnError and abort init"
        )


@pytest.mark.asyncio
async def test_shared_helper_orders_served_index_after_column():
    """The single source of truth adds the served columns before indexing them."""
    conn, statements = _capturing_conn()
    await ensure_api_logs_schema(conn)
    _assert_served_columns_before_index(statements)


@pytest.mark.asyncio
async def test_helper_skips_migration_ddl_when_schema_current():
    """Steady-state startup issues no ALTER/CREATE INDEX/DROP — only CREATE TABLE.

    When the catalog snapshot reports every column and index already present,
    the helper must not emit lock-taking migration DDL, so a restart under load
    can't starve the ``api_logs`` lock queue.
    """
    conn, statements = _capturing_conn()
    column_rows = [{"attname": name} for name, _ in _API_LOGS_COLUMN_MIGRATIONS]
    index_rows = [{"indexname": name} for name, _ in _API_LOGS_INDEXES]
    # First fetch = columns, second fetch = indexes (order matches the helper).
    conn.fetch = AsyncMock(side_effect=[column_rows, index_rows])

    await ensure_api_logs_schema(conn)

    migrations = [
        s for s in statements if s.strip().startswith(("ALTER TABLE", "CREATE INDEX", "DROP INDEX"))
    ]
    assert migrations == [], f"expected no migration DDL, got: {migrations}"
    # The two idempotent CREATE TABLE IF NOT EXISTS statements still run.
    assert all("CREATE TABLE IF NOT EXISTS" in s for s in statements)


@pytest.mark.asyncio
async def test_create_table_includes_served_columns():
    """Fresh installs start complete: the CREATE TABLE carries the served cols."""
    conn, statements = _capturing_conn()
    await ensure_api_logs_schema(conn)
    create = next(s for s in statements if "CREATE TABLE IF NOT EXISTS api_logs" in s)
    assert "served_model_id TEXT" in create
    assert "served_endpoint_id TEXT" in create


@pytest.mark.asyncio
async def test_database_logger_adds_served_columns_before_index():
    """The bootstrap schema builder must add the served columns (ordered)."""
    pool, statements = _capturing_pool()
    db = DatabaseLogger({"dsn": "postgres://ignored"})
    db.pool = pool
    await db._create_tables()
    _assert_served_columns_before_index(statements)


@pytest.mark.asyncio
async def test_postgres_log_store_initialize_orders_served_index_after_column():
    """``PostgresLogStore.initialize`` must not index a not-yet-added column."""
    pool, statements = _capturing_pool()
    store = PostgresLogStore(pool)
    await store.initialize()
    _assert_served_columns_before_index(statements)


@pytest.mark.asyncio
async def test_both_initializers_emit_identical_api_logs_ddl():
    """Both callers share one source of truth, so their api_logs DDL matches.

    This is the drift guard for the original outage: a column added to one
    initializer but not the other would diverge these two sequences.
    """
    db_pool, db_statements = _capturing_pool()
    db = DatabaseLogger({"dsn": "postgres://ignored"})
    db.pool = db_pool
    await db._create_tables()

    store_pool, store_statements = _capturing_pool()
    store = PostgresLogStore(store_pool)
    await store.initialize()

    assert _api_logs_statements(db_statements) == _api_logs_statements(store_statements)
