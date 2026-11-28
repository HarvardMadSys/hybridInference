"""``0028_key_authorization_codes``: the codes a user approves for an application."""

from __future__ import annotations

import importlib

import asyncpg
import pytest

from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

REVISION = "0028_key_authorization_codes"
TABLE = "api_key_authorization_codes"

_INSERT = (
    "INSERT INTO api_key_authorization_codes "
    "(code_hash, user_id, callback_url, code_challenge, expires_at) "
    "VALUES ($1, $2, 'https://app.example.org/callback', 'challenge', "
    "NOW() + INTERVAL '3 minutes')"
)


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def test_the_table_arrives_empty_and_existing_accounts_are_untouched(scratch):
    await mig.upgrade(scratch, "0027_openinfra_archives")
    async with mig.connected(scratch) as conn:
        before = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", TABLE)
        await conn.execute("INSERT INTO users (id, email) VALUES ('u-old', 'old@example.edu')")

    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        columns = {
            row["column_name"]: (row["data_type"], row["is_nullable"])
            for row in await conn.fetch(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name = $1",
                TABLE,
            )
        }
        rows = await conn.fetchval(f"SELECT COUNT(*) FROM {TABLE}")
        user = await conn.fetchrow("SELECT email, status FROM users WHERE id = 'u-old'")
        indexes = {
            row["indexname"]
            for row in await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename = $1", TABLE
            )
        }

    assert before is False
    assert columns == {
        "code_hash": ("text", "NO"),
        "user_id": ("text", "NO"),
        "callback_url": ("text", "NO"),
        "code_challenge": ("text", "NO"),
        "created_at": ("timestamp with time zone", "NO"),
        "expires_at": ("timestamp with time zone", "NO"),
        "consumed_at": ("timestamp with time zone", "YES"),
    }
    assert rows == 0
    assert dict(user) == {"email": "old@example.edu", "status": "active"}
    assert {
        "idx_api_key_authorization_codes_expires_at",
        "idx_api_key_authorization_codes_user_id",
    } <= indexes


async def test_a_code_is_unique_belongs_to_an_account_and_leaves_with_it(scratch):
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        await conn.execute("INSERT INTO users (id, email) VALUES ('u-1', 'a@example.edu')")
        await conn.execute("INSERT INTO users (id, email) VALUES ('u-2', 'b@example.edu')")
        await conn.execute(_INSERT, "hash-1", "u-1")
        await conn.execute(_INSERT, "hash-2", "u-2")

        # One row per code.
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(_INSERT, "hash-1", "u-2")
        # Only for an account that exists.
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(_INSERT, "hash-3", "u-missing")
        created = await conn.fetchrow(
            "SELECT created_at IS NOT NULL AS stamped, consumed_at FROM "
            "api_key_authorization_codes WHERE code_hash = 'hash-1'"
        )

        await conn.execute("DELETE FROM users WHERE id = 'u-1'")
        remaining = [row["code_hash"] for row in await conn.fetch(f"SELECT code_hash FROM {TABLE}")]

    assert dict(created) == {"stamped": True, "consumed_at": None}
    assert remaining == ["hash-2"]


async def test_its_statements_can_run_again(scratch):
    """Going back up after a downgrade, which keeps the table, still works."""
    await mig.upgrade(scratch)
    revision = importlib.import_module(f"migrations.versions.{REVISION}")
    async with mig.connected(scratch) as conn:
        await conn.execute("INSERT INTO users (id, email) VALUES ('u-1', 'a@example.edu')")
        await conn.execute(_INSERT, "hash-kept", "u-1")
        for statement in revision.STATEMENTS:
            await conn.execute(statement)
        kept = await conn.fetchval(f"SELECT COUNT(*) FROM {TABLE}")
    assert kept == 1
