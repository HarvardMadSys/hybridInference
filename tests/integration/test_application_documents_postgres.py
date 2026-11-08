"""Application-document upgrade and transactional routing change counters."""

from __future__ import annotations

import json

import pytest

from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


async def test_upgrade_preserves_catalog_and_lifts_secrets():
    async with mig.scratch_database() as database:
        await mig.upgrade(database, "0011_trim_oversized_preferences")
        async with mig.connected(database) as conn:
            await conn.execute(
                "INSERT INTO route_catalog(kind,body,source) VALUES ('models',$1,'yaml_import')",
                "models:\n  - id: existing\n    api_key: private-key\n",
            )
        await mig.upgrade(database)
        async with mig.connected(database) as conn:
            rows = {
                row["key"]: row
                for row in await conn.fetch("SELECT key,value,secret FROM app_config")
            }
            document = json.loads(rows["MODEL_CATALOG"]["value"])
            reference = document["models"][0]["api_key"]
            secret = rows[reference[2:-1]]
            assert secret["secret"] and secret["value"] == "private-key"
            assert "private-key" not in rows["MODEL_CATALOG"]["value"]
            assert (
                await conn.fetchval("SELECT body FROM route_catalog WHERE kind='models'")
                == "models:\n  - id: existing\n    api_key: private-key\n"
            )


async def test_upgrade_does_not_overwrite_administrator_catalog():
    async with mig.scratch_database() as database:
        await mig.upgrade(database, "0011_trim_oversized_preferences")
        async with mig.connected(database) as conn:
            await conn.execute(
                "INSERT INTO route_catalog(kind,body,source) VALUES ('models','models: []','yaml_import')"
            )
            await conn.execute(
                "INSERT INTO app_config(key,value,secret,source) VALUES ('MODEL_CATALOG','{}',false,'admin')"
            )
        await mig.upgrade(database)
        async with mig.connected(database) as conn:
            assert (
                await conn.fetchval("SELECT value FROM app_config WHERE key='MODEL_CATALOG'")
                == "{}"
            )


async def test_routing_counter_is_scoped_and_rolls_back_with_the_write():
    async with mig.scratch_database() as database:
        await mig.upgrade(database)
        async with mig.connected(database) as conn:
            await conn.execute(
                "INSERT INTO site_settings(key,value,value_type) VALUES ('model_router_strategy:one','fixed','str')"
            )
            first = await conn.fetchval(
                "SELECT revision FROM routing_config_revisions WHERE model_id='one'"
            )
            assert first == 1
            transaction = conn.transaction()
            await transaction.start()
            await conn.execute(
                "UPDATE site_settings SET value='routewise' WHERE key='model_router_strategy:one'"
            )
            await transaction.rollback()
            assert (
                await conn.fetchval(
                    "SELECT revision FROM routing_config_revisions WHERE model_id='one'"
                )
                == first
            )
            await conn.execute(
                "INSERT INTO site_settings(key,value,value_type) VALUES ('unrelated','true','bool')"
            )
            assert await conn.fetchval("SELECT COUNT(*) FROM routing_config_revisions") == 1


async def test_routing_write_recreates_a_missing_source_history():
    async with mig.scratch_database() as database:
        await mig.upgrade(database)
        async with mig.connected(database) as conn:
            await conn.execute("DELETE FROM config_source_revisions WHERE source='routing'")
            await conn.execute(
                "INSERT INTO site_settings(key,value,value_type) VALUES ('model_router_strategy:one','fixed','str')"
            )
            assert (
                await conn.fetchval(
                    "SELECT revision FROM config_source_revisions WHERE source='routing'"
                )
                == 1
            )
            assert (
                await conn.fetchval(
                    "SELECT revision FROM routing_config_revisions WHERE model_id='one'"
                )
                == 1
            )


async def test_catalog_saves_remain_readable_by_a_previous_image():
    async with mig.scratch_database() as database:
        await mig.upgrade(database)
        async with mig.connected(database) as conn:
            await conn.execute(
                "INSERT INTO app_config(key,value,secret,source) VALUES ('MODEL_CATALOG',$1,false,'admin')",
                '{"models": [{"id": "new"}]}',
            )
            assert (
                json.loads(
                    await conn.fetchval("SELECT body FROM route_catalog WHERE kind='models'")
                )["models"][0]["id"]
                == "new"
            )
            await conn.execute(
                "UPDATE app_config SET source='reset',value='' WHERE key='MODEL_CATALOG'"
            )
            assert (
                json.loads(
                    await conn.fetchval("SELECT body FROM route_catalog WHERE kind='models'")
                )
                == {}
            )
