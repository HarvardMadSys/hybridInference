"""Test that the DB migration correctly upgrades old 4-role → 3-role hierarchy.

This simulates a real upgrade path: a database that already has the old
``users_role_check`` constraint allowing ``('free','internal_group','developer','admin')``
and rows with those old role values.  After ``_create_tables()`` runs, all rows
must have been migrated to the new 3-role set and the new constraint must be in place.

Requires a running PostgreSQL test database (see TEST_DB_* env vars).
Run with: make test-db
"""

import pytest
import pytest_asyncio
from ulid import ULID

from serving.storage.database import DatabaseLogger
from test.fixtures.auth_factories import create_test_user

pytest_plugins = ["test.servers.conftest_auth"]

pytestmark = pytest.mark.dbtest


@pytest_asyncio.fixture
async def migration_db(auth_db_logger: DatabaseLogger):
    """Prepare a DB with old 4-role constraint and sample users.

    The auth_db_logger fixture already calls _create_tables() once (which
    installs the *new* constraint).  We revert to the old constraint and
    insert rows with legacy role values so we can test the upgrade path.
    """
    if not auth_db_logger or not auth_db_logger.pool:
        pytest.skip("PostgreSQL auth test database is not available.")

    async with auth_db_logger.pool.acquire() as conn:
        # Clean up
        await conn.execute("DELETE FROM auth_sessions")
        await conn.execute("DELETE FROM api_keys WHERE account_id IS NOT NULL")
        await conn.execute("DELETE FROM users")

        # Revert constraint to old 4-role version
        await conn.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check")
        await conn.execute("""
            ALTER TABLE users
            ADD CONSTRAINT users_role_check
            CHECK (role IN ('free', 'internal_group', 'developer', 'admin'))
        """)

        # Insert users with old role values
        for role in ("free", "internal_group", "developer", "admin"):
            user = create_test_user()
            await conn.execute(
                """
                INSERT INTO users (id, email, password_hash, user_name, status, email_verified, role)
                VALUES ($1, $2, $3, $4, 'active', TRUE, $5)
                """,
                str(ULID()),
                f"{role}-user@test.example.com",
                user["password_hash"],
                f"Test {role}",
                role,
            )

    yield auth_db_logger

    # Clean up after test
    async with auth_db_logger.pool.acquire() as conn:
        await conn.execute("DELETE FROM auth_sessions")
        await conn.execute("DELETE FROM api_keys WHERE account_id IS NOT NULL")
        await conn.execute("DELETE FROM users")


class TestRoleMigration:
    """Test the in-place migration from 4-role to 3-role hierarchy."""

    @pytest.mark.asyncio
    async def test_create_tables_migrates_old_roles(self, migration_db: DatabaseLogger) -> None:
        """_create_tables() should migrate internal_group/developer → internal."""
        pool = migration_db.pool
        assert pool is not None

        # Verify we have old roles before migration
        async with pool.acquire() as conn:
            old_roles = {r["role"] for r in await conn.fetch("SELECT DISTINCT role FROM users")}
        assert "internal_group" in old_roles
        assert "developer" in old_roles

        # Run migration (same as service restart)
        await migration_db._create_tables()

        async with pool.acquire() as conn:
            # All old roles must be gone
            rows = await conn.fetch("SELECT email, role FROM users ORDER BY email")
            roles = {r["role"] for r in rows}

        assert roles == {"free", "internal", "admin"}

        # Verify the specific mappings
        role_by_email = {r["email"]: r["role"] for r in rows}
        assert role_by_email["internal_group-user@test.example.com"] == "internal"
        assert role_by_email["developer-user@test.example.com"] == "internal"
        assert role_by_email["free-user@test.example.com"] == "free"
        assert role_by_email["admin-user@test.example.com"] == "admin"

    @pytest.mark.asyncio
    async def test_new_constraint_rejects_old_roles(self, migration_db: DatabaseLogger) -> None:
        """After migration, inserting old role values should be rejected."""
        await migration_db._create_tables()

        pool = migration_db.pool
        assert pool is not None

        import asyncpg

        async with pool.acquire() as conn:
            for bad_role in ("internal_group", "developer"):
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute(
                        """
                        INSERT INTO users (id, email, password_hash, user_name, status, email_verified, role)
                        VALUES ($1, $2, 'hash', 'test', 'active', TRUE, $3)
                        """,
                        str(ULID()),
                        f"reject-{bad_role}@test.example.com",
                        bad_role,
                    )

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

    @pytest.mark.asyncio
    async def test_migration_is_idempotent(self, migration_db: DatabaseLogger) -> None:
        """Running _create_tables() twice should not fail or corrupt data."""
        await migration_db._create_tables()
        await migration_db._create_tables()  # second run — must be safe

        pool = migration_db.pool
        assert pool is not None

        async with pool.acquire() as conn:
            roles = {r["role"] for r in await conn.fetch("SELECT DISTINCT role FROM users")}
        assert roles == {"free", "internal", "admin"}
