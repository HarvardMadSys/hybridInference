"""Environment-key state participates in the same committed provider revision."""

from __future__ import annotations

import pytest

from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


async def test_env_key_disable_enable_and_tier_edits_advance_provider_counter():
    async with mig.scratch_database() as database:
        await mig.upgrade(database)
        async with mig.connected(database) as conn:
            await conn.execute(
                "INSERT INTO disabled_provider_env_keys(provider,key_hash,key_prefix) VALUES ('sync','hash','prefix')"
            )
            disabled = await conn.fetchval(
                "SELECT revision FROM routing_config_revisions WHERE model_id='@providers'"
            )
            await conn.execute("DELETE FROM disabled_provider_env_keys WHERE provider='sync'")
            enabled = await conn.fetchval(
                "SELECT revision FROM routing_config_revisions WHERE model_id='@providers'"
            )
            assert enabled > disabled
            await conn.execute(
                "INSERT INTO provider_env_key_min_roles(provider,key_hash,key_prefix,min_role) VALUES ('sync','hash','prefix','pro')"
            )
            reserved = await conn.fetchval(
                "SELECT revision FROM routing_config_revisions WHERE model_id='@providers'"
            )
            assert reserved > enabled
            await conn.execute("DELETE FROM provider_env_key_min_roles WHERE provider='sync'")
            assert (
                await conn.fetchval(
                    "SELECT revision FROM routing_config_revisions WHERE model_id='@providers'"
                )
                > reserved
            )
