"""``0007_request_volume_by_source``: the batch worker's counts on ``request_volume_hourly``.

Six defaulted ``batch_*`` columns and a ``definition``. The previous release,
which names only its own columns, must keep storing hours, and the hours it
stores must read as not stored here, to be counted again.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest

from serving.servers.routers.admin import request_volume
from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

_BATCH_COUNTS = tuple(f"batch_{name}" for name in request_volume.COUNT_COLUMNS[:6])

# What the previous release's fill writes, column for column.
_PREVIOUS_FILL = (
    "INSERT INTO request_volume_hourly (hour_bucket, request_count, success_count, "
    "error_count, client_disconnect_count, latency_count, latency_sum_ms, completed_at) "
    "VALUES ($1, 3, 2, 1, 0, 3, 300, NOW())"
)

_HOUR = datetime(2026, 10, 1, 9, tzinfo=UTC)


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def test_stored_hours_gain_zero_batch_counts_and_no_fingerprint(scratch):
    await mig.upgrade(scratch, "0006_drop_batch_finished_index")
    async with mig.connected(scratch) as conn:
        await conn.execute(_PREVIOUS_FILL, _HOUR)

    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        columns = {
            r["column_name"]: (r["data_type"], r["is_nullable"], r["column_default"])
            for r in await conn.fetch(
                "SELECT column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns WHERE table_name = 'request_volume_hourly'"
            )
        }
        row = await conn.fetchrow("SELECT * FROM request_volume_hourly")

    for column in _BATCH_COUNTS:
        assert columns[column] == ("bigint", "NO", "0"), column
    assert columns["definition"] == ("text", "NO", "''::text")
    assert row["request_count"] == 3
    assert all(row[column] == 0 for column in _BATCH_COUNTS)
    assert row["definition"] == ""


async def test_the_previous_release_still_stores_hours_and_this_one_counts_them_again(scratch):
    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        await conn.execute(_PREVIOUS_FILL, _HOUR)
        # Not stored, as far as this release reads or fills.
        assert await conn.fetch(request_volume._STORED_OF_SQL, [_HOUR]) == []
        assert await conn.fetch(request_volume._STORED_SQL, _HOUR, _HOUR.replace(hour=10)) == []

        # Counted again, over the rows the hour holds (none here), in place.
        assert await request_volume.fill_hours(conn, [_HOUR]) == 1
        row = await conn.fetchrow("SELECT * FROM request_volume_hourly")
    assert row["definition"] == request_volume.DEFINITION
    assert row["request_count"] == 0


async def test_its_statements_can_run_again(scratch):
    """Going back up after a downgrade, which keeps the columns, still works."""
    await mig.upgrade(scratch)
    revision = importlib.import_module("migrations.versions.0007_request_volume_by_source")
    async with mig.connected(scratch) as conn:
        for statement in revision.STATEMENTS:
            await conn.execute(statement)
