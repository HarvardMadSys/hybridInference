"""``0027_openinfra_archives``: per-second sample buckets and the archive record."""

from __future__ import annotations

import importlib

import asyncpg
import pytest

from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

REVISION = "0027_openinfra_archives"
T0 = 1_800_000_000_000


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def _six_second_samples(conn) -> None:
    """Samples as the release before stored them: ``bucket`` is ``ts / 6000``."""
    for table, key, value in (
        ("openinfra_gpu_samples", "gpu_id", "n:0"),
        ("openinfra_engine_samples", "engine_id", "e"),
        ("openinfra_cpu_samples", "node_id", "n"),
    ):
        for ts in (T0 + 500, T0 + 6_200, T0 + 12_900):
            await conn.execute(
                f"INSERT INTO {table} ({key}, bucket, ts) VALUES ($1, $2, $3)",
                value,
                ts // 6_000,
                ts,
            )


async def test_six_second_samples_get_their_seconds(scratch):
    await mig.upgrade(scratch, "0026_openinfra")
    async with mig.connected(scratch) as conn:
        await _six_second_samples(conn)
        # A sample of its own in the last one's second, written per second.
        await conn.execute(
            "INSERT INTO openinfra_gpu_samples (gpu_id, bucket, ts, utilization_pct) "
            "VALUES ('n:0', $1, $2, 99)",
            (T0 + 12_100) // 1_000,
            T0 + 12_100,
        )
    await mig.upgrade(scratch, REVISION)
    async with mig.connected(scratch) as conn:
        gpu = await conn.fetch(
            "SELECT bucket, ts, utilization_pct FROM openinfra_gpu_samples ORDER BY ts"
        )
        cpu = await conn.fetch("SELECT bucket, ts FROM openinfra_cpu_samples ORDER BY ts")
        archives = await conn.fetchval("SELECT COUNT(*) FROM openinfra_archives")
    # Every sample kept, in its own second; the one already held gave way.
    assert [(r["bucket"], r["ts"], r["utilization_pct"]) for r in gpu] == [
        (T0 // 1_000, T0 + 500, None),
        (T0 // 1_000 + 6, T0 + 6_200, None),
        (T0 // 1_000 + 12, T0 + 12_100, 99.0),
    ]
    assert [(r["bucket"], r["ts"]) for r in cpu] == [
        (T0 // 1_000, T0 + 500),
        (T0 // 1_000 + 6, T0 + 6_200),
        (T0 // 1_000 + 12, T0 + 12_900),
    ]
    assert archives == 0


async def test_one_archive_record_per_week(scratch):
    await mig.upgrade(scratch, REVISION)
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO openinfra_archives (week_start, location, tables, archived_at) "
            "VALUES (345600000, '/archive/1970-01-05', '{}', 1)"
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO openinfra_archives (week_start, location, tables, archived_at) "
                "VALUES (345600000, '/elsewhere', '{}', 2)"
            )


async def test_its_statements_can_run_again(scratch):
    """Going back up after a downgrade, which keeps everything, still works."""
    await mig.upgrade(scratch, "0026_openinfra")
    async with mig.connected(scratch) as conn:
        await _six_second_samples(conn)
    await mig.upgrade(scratch, REVISION)
    revision = importlib.import_module(f"migrations.versions.{REVISION}")
    async with mig.connected(scratch) as conn:
        before = await conn.fetch("SELECT gpu_id, bucket, ts FROM openinfra_gpu_samples")
        for statement in revision.STATEMENTS:
            await conn.execute(statement)
        after = await conn.fetch("SELECT gpu_id, bucket, ts FROM openinfra_gpu_samples")
    assert sorted(map(tuple, after)) == sorted(map(tuple, before))
