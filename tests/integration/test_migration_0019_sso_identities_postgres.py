"""``0019_sso_identities``: linked identities, sign-in flows and pending links."""

from __future__ import annotations

import asyncpg
import pytest

from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

TABLES = ("user_identities", "sso_flows", "sso_pending_links")


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def _columns(conn, table: str) -> set[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = $1", table
    )
    return {row["column_name"] for row in rows}


async def test_the_tables_arrive_empty_and_existing_accounts_are_untouched(scratch):
    await mig.upgrade(scratch, "0018_projects_owned_by_accounts")
    async with mig.connected(scratch) as conn:
        await conn.execute("INSERT INTO users (id, email) VALUES ('u-old', 'old@example.edu')")

    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        tables = {
            row["table_name"]
            for row in await conn.fetch(
                "SELECT table_name FROM information_schema.tables WHERE table_name = ANY($1)",
                list(TABLES),
            )
        }
        counts = [await conn.fetchval(f"SELECT COUNT(*) FROM {table}") for table in TABLES]
        user = await conn.fetchrow("SELECT email, status FROM users WHERE id = 'u-old'")
        identity_columns = await _columns(conn, "user_identities")
        flow_columns = await _columns(conn, "sso_flows")
        pending_columns = await _columns(conn, "sso_pending_links")

    assert tables == set(TABLES)
    assert counts == [0, 0, 0]
    assert dict(user) == {"email": "old@example.edu", "status": "active"}
    assert identity_columns == {
        "id",
        "user_id",
        "provider_id",
        "issuer",
        "subject",
        "email",
        "created_at",
        "last_login_at",
    }
    assert flow_columns == {
        "state_hash",
        "binding_hash",
        "provider_id",
        "purpose",
        "return_to",
        "protocol_state",
        "user_id",
        "sid",
        "reauth_purpose",
        "created_at",
        "expires_at",
        "consumed_at",
        "reauth_verified_at",
        "reauth_used_at",
    }
    assert pending_columns == {
        "token_hash",
        "provider_id",
        "issuer",
        "subject",
        "email",
        "created_at",
        "expires_at",
        "consumed_at",
    }


async def test_an_identity_is_unique_and_leaves_with_its_account(scratch):
    await mig.upgrade(scratch)
    insert = (
        "INSERT INTO user_identities (id, user_id, provider_id, issuer, subject) "
        "VALUES ($1, $2, 'idp', $3, $4)"
    )
    async with mig.connected(scratch) as conn:
        await conn.execute("INSERT INTO users (id, email) VALUES ('u-1', 'a@example.edu')")
        await conn.execute("INSERT INTO users (id, email) VALUES ('u-2', 'b@example.edu')")
        await conn.execute(insert, "i-1", "u-1", "https://idp.example.edu", "sub-1")

        # One identity signs in to one account only.
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(insert, "i-2", "u-2", "https://idp.example.edu", "sub-1")
        # One account holds one identity per issuer.
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(insert, "i-3", "u-1", "https://idp.example.edu", "sub-2")
        # A second issuer is a second identity.
        await conn.execute(insert, "i-4", "u-1", "https://other.example.edu", "sub-1")

        await conn.execute("DELETE FROM users WHERE id = 'u-1'")
        remaining = await conn.fetchval("SELECT COUNT(*) FROM user_identities")

    assert remaining == 0


async def test_a_flow_accepts_only_the_three_purposes(scratch):
    await mig.upgrade(scratch)
    insert = (
        "INSERT INTO sso_flows (state_hash, binding_hash, provider_id, purpose, return_to, "
        "expires_at) VALUES ($1, 'b', 'idp', $2, '/dashboard', NOW() + INTERVAL '10 minutes')"
    )
    async with mig.connected(scratch) as conn:
        for index, purpose in enumerate(("login", "reauth", "link")):
            await conn.execute(insert, f"s-{index}", purpose)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(insert, "s-bad", "signup")
        state = await conn.fetchval("SELECT protocol_state::text FROM sso_flows LIMIT 1")

    assert state == "{}"
