"""``0008_route_batch_policies``: the table of which routes batch work may use."""

from __future__ import annotations

import importlib

import pytest

from migrations.compat import read_state
from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

PREVIOUS = "0007_request_volume_by_source"
REVISION = "0008_route_batch_policies"


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def test_the_table_is_created_keyed_by_model_and_route(scratch):
    await mig.upgrade(scratch, PREVIOUS)
    async with mig.connected(scratch) as conn:
        assert await conn.fetchval("SELECT to_regclass('provider_route_batch_policies')") is None

    await mig.upgrade(scratch, REVISION)

    async with mig.connected(scratch) as conn:
        columns = {
            r["column_name"]: (r["data_type"], r["is_nullable"])
            for r in await conn.fetch(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'provider_route_batch_policies'"
            )
        }
        key = await conn.fetch(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'provider_route_batch_policies'::regclass AND i.indisprimary"
        )
        assert (await read_state(conn)).revision == REVISION
    assert columns == {
        "model_id": ("text", "NO"),
        "route_id": ("text", "NO"),
        "batch_allowed": ("boolean", "NO"),
        "updated_at": ("timestamp with time zone", "NO"),
        "updated_by": ("text", "YES"),
    }
    assert {row["attname"] for row in key} == {"model_id", "route_id"}


async def test_its_statements_can_run_again(scratch):
    """Going back up after a downgrade, which keeps the table, still works."""
    await mig.upgrade(scratch)
    revision = importlib.import_module(f"migrations.versions.{REVISION}")
    async with mig.connected(scratch) as conn:
        for statement in revision.STATEMENTS:
            await conn.execute(statement)
