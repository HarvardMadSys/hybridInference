"""Schema-init guards for the api_logs served-model / served-endpoint columns.

Regression coverage for the boot-time schema builders. Two invariants must
hold, or ``log_request`` inserts fail in production with
``UndefinedColumnError: column "served_model_id" ... does not exist``:

1. ``served_model_id`` and ``served_endpoint_id`` are actually added. The
   production ``api_logs`` schema is built by ``DatabaseLogger._create_tables``
   (run at bootstrap), *not* by ``PostgresLogStore.initialize`` (constructed but
   never initialized at boot) — so the columns must be present in the former.
2. Any index that references ``served_endpoint_id`` is created *after* the
   column is added. Creating the index first raises ``UndefinedColumnError`` and
   aborts table creation, leaving ``api_logs`` permanently without the columns.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.database import DatabaseLogger
from serving.storage.postgres_log import PostgresLogStore


def _capturing_pool() -> tuple[MagicMock, list[str]]:
    """Return ``(pool, statements)`` recording every SQL string executed."""
    statements: list[str] = []
    conn = MagicMock()

    async def _execute(sql: str, *_args: object) -> str:
        statements.append(sql)
        return "OK"

    conn.execute = AsyncMock(side_effect=_execute)
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire_cm
    return pool, statements


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
async def test_database_logger_adds_served_columns_before_index():
    """The production schema builder must add the served columns (ordered)."""
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
