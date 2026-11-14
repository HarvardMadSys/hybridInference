"""``0017_revoke_scoped_keys``: scoped keys end with the gate that confined them."""

from __future__ import annotations

import pytest

from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def _keys(conn) -> dict[str, tuple[str, bool]]:
    return {
        row["key_hash"]: (row["status"], row["revoked_at"] is not None)
        for row in await conn.fetch("SELECT key_hash, status, revoked_at FROM api_keys")
    }


async def test_only_active_scoped_keys_are_revoked(scratch):
    await mig.upgrade(scratch, "0016_agent_provisioning")
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO api_keys "
            "(key_hash, key_prefix, user_id, account_id, status, credential_scope, revoked_at) "
            "VALUES "
            "('h-plain', 'hyi-plain', 'u-1', 'u-1', 'active', NULL, NULL), "
            "('h-scoped', 'hyi-scoped', 'u-1', 'u-1', 'active', 'agent', NULL), "
            "('h-other', 'hyi-other', 'u-2', 'u-2', 'active', 'something-else', NULL), "
            "('h-gone', 'hyi-gone', 'u-2', 'u-2', 'revoked', 'agent', '2026-11-13T00:00:00Z')"
        )
        gone_at = await conn.fetchval("SELECT revoked_at FROM api_keys WHERE key_hash = 'h-gone'")

    await mig.upgrade(scratch, "0017_revoke_scoped_keys")

    async with mig.connected(scratch) as conn:
        keys = await _keys(conn)
        assert keys == {
            "h-plain": ("active", False),
            "h-scoped": ("revoked", True),
            "h-other": ("revoked", True),
            "h-gone": ("revoked", True),
        }
        # A key revoked earlier keeps the time it was revoked at.
        assert gone_at == await conn.fetchval(
            "SELECT revoked_at FROM api_keys WHERE key_hash = 'h-gone'"
        )
        # The column and the tables of 0016 stay for the previous release.
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_name = 'api_keys' AND column_name = 'credential_scope'"
            )
            == 1
        )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_name IN ('agent_invite_tokens', 'agent_provisioned_accounts')"
            )
            == 2
        )


async def test_a_database_without_scoped_keys_is_left_as_it_is(scratch):
    await mig.upgrade(scratch, "0016_agent_provisioning")
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, user_id, account_id) "
            "VALUES ('h-plain', 'hyi-plain', 'u-1', 'u-1')"
        )

    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        assert await _keys(conn) == {"h-plain": ("active", False)}
