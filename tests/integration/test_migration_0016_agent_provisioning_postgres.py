"""``0016_agent_provisioning``: key credential scope + invitation tables."""

from __future__ import annotations

import pytest

from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def test_an_existing_key_gains_no_scope_and_the_tables_arrive_empty(scratch):
    await mig.upgrade(scratch, "0015_projects")
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, user_id, account_id) "
            "VALUES ('h-old', 'hyi-old', 'u-old', 'u-old')"
        )

    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        column = await conn.fetchrow(
            "SELECT data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'api_keys' AND column_name = 'credential_scope'"
        )
        scope = await conn.fetchval(
            "SELECT credential_scope FROM api_keys WHERE key_hash = 'h-old'"
        )
        tables = {
            row["table_name"]
            for row in await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name IN ('agent_invite_tokens', 'agent_provisioned_accounts')"
            )
        }
        invite_columns = {
            row["column_name"]
            for row in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'agent_invite_tokens'"
            )
        }
    assert dict(column) == {"data_type": "text", "is_nullable": "YES", "column_default": None}
    assert scope is None
    assert tables == {"agent_invite_tokens", "agent_provisioned_accounts"}
    assert invite_columns == {
        "token_hash",
        "agent_account_id",
        "email",
        "created_at",
        "expires_at",
        "consumed_at",
        "superseded_at",
    }
