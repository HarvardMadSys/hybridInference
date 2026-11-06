"""``0004_user_usage_sums``: the dashboard's sums on ``user_usage_daily``.

Seven defaulted columns on the rows and a ``usage_definition`` on the coverage.
The previous release, which names only its own columns, must keep filling days,
and the days it fills must read as not stored for the sums.
"""

from __future__ import annotations

import importlib
from datetime import date

import pytest

from serving.storage import user_usage_rollup
from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

_SUMS = (
    "cost_usd",
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "reasoning_tokens_known",
)

# What the previous release's fill writes, column for column.
_PREVIOUS_FILL = (
    "INSERT INTO user_usage_daily (user_id, day, request_count, total_tokens, turns_sum, "
    "turns_count, user_turns_sum, user_turns_count, early_requests, early_ask_requests) "
    "VALUES ($1, $2, 3, 30, 0, 0, 0, 0, 0, 0)"
)
_PREVIOUS_MARK = (
    "INSERT INTO user_usage_daily_coverage (day, definition) VALUES ($1, $2) "
    "ON CONFLICT (day) DO UPDATE SET definition = EXCLUDED.definition, completed_at = NOW()"
)


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def test_stored_days_gain_zero_sums_and_no_usage_fingerprint(scratch):
    await mig.upgrade(scratch, "0003_batch_no_expiry")
    async with mig.connected(scratch) as conn:
        await conn.execute(_PREVIOUS_FILL, "u-old", date(2026, 10, 1))
        await conn.execute(_PREVIOUS_MARK, date(2026, 10, 1), user_usage_rollup.DEFINITION)

    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        columns = {
            r["column_name"]: (r["data_type"], r["is_nullable"], r["column_default"])
            for r in await conn.fetch(
                "SELECT column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_name IN ('user_usage_daily', 'user_usage_daily_coverage')"
            )
        }
        row = await conn.fetchrow("SELECT * FROM user_usage_daily WHERE user_id = 'u-old'")
        coverage = await conn.fetchrow("SELECT * FROM user_usage_daily_coverage")

    assert columns["cost_usd"] == ("numeric", "NO", "0")
    for column in _SUMS[1:]:
        assert columns[column] == ("bigint", "NO", "0"), column
    assert columns["usage_definition"] == ("text", "NO", "''::text")
    assert all(row[column] == 0 for column in _SUMS)
    assert row["request_count"] == 3
    # The admin columns' day is still stored; the sums' is not.
    assert coverage["definition"] == user_usage_rollup.DEFINITION
    assert coverage["usage_definition"] == ""


async def test_the_previous_release_still_fills_days(scratch):
    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        await conn.execute(_PREVIOUS_FILL, "u-new", date(2026, 10, 2))
        await conn.execute(_PREVIOUS_MARK, date(2026, 10, 2), user_usage_rollup.DEFINITION)
        coverage = await conn.fetchrow(
            "SELECT definition, usage_definition FROM user_usage_daily_coverage"
        )

    # Which this release reads as stored for the admin columns and counts live
    # for the sums, until its own backfill fills the day again.
    assert dict(coverage) == {
        "definition": user_usage_rollup.DEFINITION,
        "usage_definition": "",
    }


async def test_its_statements_can_run_again(scratch):
    """Going back up after a downgrade, which keeps the columns, still works."""
    await mig.upgrade(scratch)
    revision = importlib.import_module("migrations.versions.0004_user_usage_sums")
    async with mig.connected(scratch) as conn:
        for statement in revision.STATEMENTS:
            await conn.execute(statement)
