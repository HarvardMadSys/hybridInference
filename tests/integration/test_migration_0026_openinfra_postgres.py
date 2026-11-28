"""``0026_openinfra``: the OpenInfra metrics tables."""

from __future__ import annotations

import importlib

import asyncpg
import pytest

from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

REVISION = "0026_openinfra"

TABLES = {
    "openinfra_nodes",
    "openinfra_gpus",
    "openinfra_engines",
    "openinfra_cpu_samples",
    "openinfra_gpu_samples",
    "openinfra_engine_samples",
    "openinfra_request_events",
    "openinfra_gpu_rollups",
    "openinfra_engine_rollups",
    "openinfra_cpu_rollups",
}


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def test_the_tables_arrive_empty(scratch):
    await mig.upgrade(scratch, "0025_service_accounts")
    async with mig.connected(scratch) as conn:
        before = await conn.fetchval(
            "SELECT COUNT(*) FROM pg_tables WHERE tablename LIKE 'openinfra%'"
        )
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        found = {
            row["tablename"]
            for row in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE tablename LIKE 'openinfra%'"
            )
        }
        rows = {table: await conn.fetchval(f"SELECT COUNT(*) FROM {table}") for table in found}
    assert before == 0
    assert found == TABLES
    assert set(rows.values()) == {0}


async def test_one_sample_per_bucket_and_one_rollup_per_bucket(scratch):
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO openinfra_gpu_samples (gpu_id, bucket, ts) VALUES ('n:0', 7, 42000)"
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO openinfra_gpu_samples (gpu_id, bucket, ts) VALUES ('n:0', 7, 43000)"
            )
        await conn.execute(
            "INSERT INTO openinfra_engine_rollups (grain, bucket_ts, engine_id, samples) "
            "VALUES (1200000, 0, 'e', 1)"
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO openinfra_engine_rollups (grain, bucket_ts, engine_id, samples) "
                "VALUES (1200000, 0, 'e', 2)"
            )
        event = await conn.fetchval(
            "INSERT INTO openinfra_request_events (engine_id, ts) VALUES ('e', 1) RETURNING id"
        )
    assert event == 1


async def test_its_statements_can_run_again(scratch):
    """Going back up after a downgrade, which keeps the tables, still works."""
    await mig.upgrade(scratch)
    revision = importlib.import_module(f"migrations.versions.{REVISION}")
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO openinfra_nodes (id, hostname, last_seen_at, created_at) "
            "VALUES ('n', 'n', 1, 1)"
        )
        for statement in revision.STATEMENTS:
            await conn.execute(statement)
        kept = await conn.fetchval("SELECT COUNT(*) FROM openinfra_nodes")
    assert kept == 1
