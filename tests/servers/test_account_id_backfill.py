"""Tests for the legacy ``api_keys.account_id`` backfill (issue #630).

Keys created before the ``account_id`` column existed have ``account_id IS
NULL``. The dashboard list/create/regenerate paths key off ``account_id``, so
these users saw "No API keys yet" and hit a 500 on **Create API Key**.
``_create_tables()`` now backfills ``account_id = user_id`` so the keys become
visible and the ``user_id`` uniqueness constraint reconciles with the
``account_id`` one. ``create_key`` also converts the unique violation into a
clean ``DuplicateAPIKeyError`` (409) instead of a raw 500.

Requires a running PostgreSQL test database (see TEST_DB_* env vars).
Run with: make test-db
"""

import pytest
import pytest_asyncio

from serving.exceptions import DuplicateAPIKeyError
from serving.storage.database import DatabaseLogger
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures.auth_factories import create_test_user

pytest_plugins = ["tests.servers.conftest_auth"]

pytestmark = pytest.mark.dbtest


async def _insert_user(conn, **overrides) -> str:
    """Insert a test user row and return its id."""
    user = create_test_user(**overrides)
    await conn.execute(
        """
        INSERT INTO users (id, email, password_hash, user_name, status, email_verified, role)
        VALUES ($1, $2, $3, $4, 'active', TRUE, 'free')
        """,
        user["id"],
        user["email"],
        user["password_hash"],
        user["user_name"],
    )
    return user["id"]


async def _insert_key(conn, *, user_id, account_id, key_prefix, status="active") -> None:
    """Insert an api_keys row with an explicit account_id (may be None)."""
    await conn.execute(
        """
        INSERT INTO api_keys (key_hash, key_prefix, user_id, status, account_id)
        VALUES ($1, $2, $3, $4, $5)
        """,
        f"hash-{key_prefix}",
        key_prefix,
        user_id,
        status,
        account_id,
    )


@pytest_asyncio.fixture
async def backfill_db(auth_db_logger: DatabaseLogger):
    """Provide a clean DB, tidying up api_keys/users around each test."""
    if not auth_db_logger or not auth_db_logger.pool:
        pytest.skip("PostgreSQL auth test database is not available.")

    async with auth_db_logger.pool.acquire() as conn:
        await conn.execute("DELETE FROM api_keys")
        await conn.execute("DELETE FROM users")

    yield auth_db_logger

    async with auth_db_logger.pool.acquire() as conn:
        await conn.execute("DELETE FROM api_keys")
        await conn.execute("DELETE FROM users")


class TestAccountIdBackfill:
    """_create_tables() backfills legacy NULL account_id rows."""

    @pytest.mark.asyncio
    async def test_backfills_null_account_id_to_user_id(self, backfill_db: DatabaseLogger):
        """A legacy active key with account_id IS NULL gets account_id = user_id."""
        pool = backfill_db.pool
        assert pool is not None

        async with pool.acquire() as conn:
            user_id = await _insert_user(conn)
            await _insert_key(conn, user_id=user_id, account_id=None, key_prefix="legacy123")

            before = await conn.fetchval(
                "SELECT account_id FROM api_keys WHERE user_id = $1", user_id
            )
            assert before is None

        # Re-run migration (same as a service restart).
        await backfill_db._create_tables()

        async with pool.acquire() as conn:
            after = await conn.fetchval(
                "SELECT account_id FROM api_keys WHERE user_id = $1", user_id
            )
        assert after == user_id

    @pytest.mark.asyncio
    async def test_skips_orphan_keys_without_user(self, backfill_db: DatabaseLogger):
        """A NULL-account_id key whose user no longer exists is left untouched."""
        pool = backfill_db.pool
        assert pool is not None

        async with pool.acquire() as conn:
            await _insert_key(
                conn, user_id="missing-user-id", account_id=None, key_prefix="orphan123"
            )

        await backfill_db._create_tables()

        async with pool.acquire() as conn:
            account_id = await conn.fetchval(
                "SELECT account_id FROM api_keys WHERE key_prefix = $1", "orphan123"
            )
        assert account_id is None

    @pytest.mark.asyncio
    async def test_backfill_is_idempotent(self, backfill_db: DatabaseLogger):
        """Running the migration twice keeps account_id stable and does not error."""
        pool = backfill_db.pool
        assert pool is not None

        async with pool.acquire() as conn:
            user_id = await _insert_user(conn)
            await _insert_key(conn, user_id=user_id, account_id=None, key_prefix="idem1234")

        await backfill_db._create_tables()
        await backfill_db._create_tables()

        async with pool.acquire() as conn:
            account_id = await conn.fetchval(
                "SELECT account_id FROM api_keys WHERE user_id = $1", user_id
            )
        assert account_id == user_id


class TestKeyLookupAndCreate:
    """get_key_by_account_or_user / create_key behavior on legacy rows."""

    @pytest.mark.asyncio
    async def test_get_key_by_account_or_user_finds_null_account_id(
        self, backfill_db: DatabaseLogger
    ):
        """The lookup matches on user_id, so it finds a NULL-account_id key."""
        pool = backfill_db.pool
        assert pool is not None
        store = PostgresOperationalStore(pool)

        async with pool.acquire() as conn:
            user_id = await _insert_user(conn)
            await _insert_key(conn, user_id=user_id, account_id=None, key_prefix="findme12")

        row = await store.get_key_by_account_or_user(user_id)
        assert row is not None
        assert row["key_prefix"] == "findme12"

        # The account-only lookup misses it — this was the original bug.
        assert await store.get_active_key_by_account(user_id) is None

    @pytest.mark.asyncio
    async def test_create_key_duplicate_active_raises_duplicate_error(
        self, backfill_db: DatabaseLogger
    ):
        """create_key colliding on idx_api_keys_user_unique → DuplicateAPIKeyError.

        Reproduces the issue #630 path: a pre-existing NULL-account_id active key
        plus an INSERT that sets account_id = user_id. The unique violation on
        user_id must surface as DuplicateAPIKeyError (clean 409), not a raw 500.
        """
        pool = backfill_db.pool
        assert pool is not None
        store = PostgresOperationalStore(pool)

        async with pool.acquire() as conn:
            user_id = await _insert_user(conn)
            await _insert_key(conn, user_id=user_id, account_id=None, key_prefix="dupe1234")

        with pytest.raises(DuplicateAPIKeyError):
            await store.create_key(
                key_hash="newhash",
                key_prefix="newprefix",
                user_id=user_id,
                account_id=user_id,
            )

    @pytest.mark.asyncio
    async def test_create_key_defaults_account_id_to_user_id(self, backfill_db: DatabaseLogger):
        """create_key without account_id must persist account_id = user_id.

        Guards the ongoing NULL-producing path: admin key creation
        (admin/api_keys.py) calls create_key without account_id, so the row
        must still default to account_id = user_id to stay visible in the
        dashboard (list/info/lookup all filter by account_id).
        """
        pool = backfill_db.pool
        assert pool is not None
        store = PostgresOperationalStore(pool)

        async with pool.acquire() as conn:
            user_id = await _insert_user(conn)

        # Note: account_id intentionally omitted, mirroring admin key creation.
        await store.create_key(
            key_hash="adminhash",
            key_prefix="adminpref",
            user_id=user_id,
        )

        async with pool.acquire() as conn:
            account_id = await conn.fetchval(
                "SELECT account_id FROM api_keys WHERE user_id = $1", user_id
            )
        assert account_id == user_id
