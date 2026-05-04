"""Regression test that the live ``users.role`` CHECK constraint accepts 'pro'.

Historically this file also covered the legacy in-place 4-role → 3-role
upgrade that ran at gateway startup via ``DatabaseLogger._create_tables()``.
That upgrade lived in idempotent CREATE/ALTER TABLE blocks; with Alembic
adopted, schema is owned by ``apps/backend/serving/storage/migrations``
and the legacy in-place migration is no longer part of the runtime path,
so those tests have been removed. Operators that still have legacy role
values in production were already migrated by the prior startup logic
before the Alembic cut-over.

Requires a running PostgreSQL test database (see TEST_DB_* env vars).
Run with: make test-db
"""

import pytest
from ulid import ULID

from serving.storage.database import DatabaseLogger

pytest_plugins = ["tests.servers.conftest_auth"]

pytestmark = pytest.mark.dbtest


class TestRoleConstraint:
    """The ``users.role`` CHECK constraint shipped in baseline must accept 'pro'."""

    @pytest.mark.asyncio
    async def test_role_check_accepts_pro(self, auth_db_logger: DatabaseLogger) -> None:
        """Regression for Task 8 critical: DB CHECK constraint must accept pro role."""
        pool = auth_db_logger.pool
        if pool is None:
            pytest.skip("Database not available")

        user_id = str(ULID())
        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO users (id, email, password_hash, user_name, role, status)
                    VALUES ($1, $2, 'x', 'pro test', 'pro', 'active')
                    """,
                    user_id,
                    f"{user_id}@test.example",
                )
                row = await conn.fetchrow("SELECT role FROM users WHERE id = $1", user_id)
                assert row["role"] == "pro"
            finally:
                await conn.execute("DELETE FROM users WHERE id = $1", user_id)
