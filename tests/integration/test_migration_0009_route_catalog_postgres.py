"""``0009_route_catalog``: the stored model registry and routing document."""

from __future__ import annotations

import importlib

import pytest

from migrations.compat import read_state
from serving.config.route_catalog import import_missing
from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

PREVIOUS = "0008_route_batch_policies"
REVISION = "0009_route_catalog"


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def test_the_table_is_created_keyed_by_kind(scratch):
    await mig.upgrade(scratch, PREVIOUS)
    async with mig.connected(scratch) as conn:
        assert await conn.fetchval("SELECT to_regclass('route_catalog')") is None

    await mig.upgrade(scratch, REVISION)

    async with mig.connected(scratch) as conn:
        columns = {
            r["column_name"]: (r["data_type"], r["is_nullable"])
            for r in await conn.fetch(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'route_catalog'"
            )
        }
        key = await conn.fetch(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'route_catalog'::regclass AND i.indisprimary"
        )
        assert (await read_state(conn)).revision == REVISION
    assert columns == {
        "kind": ("text", "NO"),
        "body": ("text", "NO"),
        "source": ("text", "NO"),
        "updated_at": ("timestamp with time zone", "NO"),
        "updated_by": ("text", "YES"),
    }
    assert {row["attname"] for row in key} == {"kind"}


async def test_its_statements_can_run_again(scratch):
    """Going back up after a downgrade, which keeps the table, still works."""
    await mig.upgrade(scratch)
    revision = importlib.import_module(f"migrations.versions.{REVISION}")
    async with mig.connected(scratch) as conn:
        for statement in revision.STATEMENTS:
            await conn.execute(statement)


async def test_import_missing_inserts_once_and_keeps_the_row(scratch):
    await mig.upgrade(scratch, REVISION)
    seed = {"models": "models: []\n", "routing": "   \n"}
    async with mig.connected(scratch) as conn:
        stored = await import_missing(conn, seed)
        assert stored == {"models": "models: []\n"}
        again = await import_missing(conn, {"models": "models:\n  - id: other\n"})
        assert again["models"] == "models: []\n"
        row = await conn.fetchrow(
            "SELECT source, updated_by FROM route_catalog WHERE kind = $1", "models"
        )
    assert row["source"] == "yaml_import"
    assert row["updated_by"] == "init"
