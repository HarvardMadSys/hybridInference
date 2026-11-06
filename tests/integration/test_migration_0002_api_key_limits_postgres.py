"""``0002_api_key_limits``: three nullable limit columns on ``api_keys``."""

from __future__ import annotations

import pytest

from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

_COLUMNS = """
    SELECT column_name, data_type, numeric_precision, numeric_scale, is_nullable,
           column_default
    FROM information_schema.columns
    WHERE table_name = 'api_keys'
      AND column_name IN ('budget_daily_usd', 'budget_monthly_usd', 'allowed_models')
    ORDER BY column_name
"""


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def test_an_existing_key_gains_the_columns_with_no_limits(scratch):
    await mig.upgrade(scratch, "0001_baseline")
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, user_id, account_id) "
            "VALUES ('h-old', 'hyi-old', 'u-old', 'u-old')"
        )

    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        columns = [dict(row) for row in await conn.fetch(_COLUMNS)]
        row = await conn.fetchrow(
            "SELECT budget_daily_usd, budget_monthly_usd, allowed_models "
            "FROM api_keys WHERE key_hash = 'h-old'"
        )
    assert columns == [
        {
            "column_name": "allowed_models",
            "data_type": "ARRAY",
            "numeric_precision": None,
            "numeric_scale": None,
            "is_nullable": "YES",
            "column_default": None,
        },
        {
            "column_name": "budget_daily_usd",
            "data_type": "numeric",
            "numeric_precision": 12,
            "numeric_scale": 4,
            "is_nullable": "YES",
            "column_default": None,
        },
        {
            "column_name": "budget_monthly_usd",
            "data_type": "numeric",
            "numeric_precision": 12,
            "numeric_scale": 4,
            "is_nullable": "YES",
            "column_default": None,
        },
    ]
    assert dict(row) == {
        "budget_daily_usd": None,
        "budget_monthly_usd": None,
        "allowed_models": None,
    }
