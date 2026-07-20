"""PostgreSQL integration tests for geo-demand hourly rollups."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio

from serving.admin.geo_demand_rollup import run_rollup
from serving.analytics.geo_demand import aggregate_geo_demand_rollup
from serving.storage.database import DatabaseLogger
from serving.utils.geo_resolver import GeoResolver

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


class FakeReader:
    def get(self, ip: str) -> dict[str, Any] | None:
        return {
            "8.8.8.8": {
                "country": {"iso_code": "US"},
                "continent": {"code": "NA"},
            }
        }.get(ip)

    def close(self) -> None:
        pass


def resolver() -> GeoResolver:
    return GeoResolver(country_reader=FakeReader(), country_provider="dbip-lite")


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = os.getenv("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN is not set; skipping database integration tests")
    return dsn


async def _truncate(pool) -> None:
    async with pool.acquire() as connection:
        await connection.execute("TRUNCATE TABLE geo_hourly_demand, geo_hourly_coverage, api_logs")


@pytest_asyncio.fixture
async def db_logger(pg_dsn: str):
    logger = DatabaseLogger({"dsn": pg_dsn}, store_full_prompts=False)
    await logger.initialize()
    assert logger.pool is not None
    await _truncate(logger.pool)
    try:
        yield logger
    finally:
        assert logger.pool is not None
        await _truncate(logger.pool)
        await logger.cleanup()


async def _insert_log(
    pool,
    *,
    request_id: str,
    timestamp: datetime,
    completion_tokens: int,
    ip: str | None,
    synthetic: bool = False,
) -> None:
    metadata = {"ip": ip}
    if synthetic:
        metadata["synthetic_probe"] = True
    async with pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO api_logs (
                request_id, model_id, provider, timestamp,
                completion_tokens, metadata
            ) VALUES ($1, 'test-model', 'test-provider', $2, $3, $4::jsonb)
            """,
            request_id,
            timestamp,
            completion_tokens,
            json.dumps(metadata),
        )


@pytest.mark.asyncio
async def test_rollup_schema_sql_and_atomic_replacement(db_logger: DatabaseLogger) -> None:
    assert db_logger.pool is not None
    pool = db_logger.pool
    hour = datetime(2026, 7, 19, 10, tzinfo=timezone.utc)

    await _insert_log(
        pool,
        request_id="geo-1",
        timestamp=hour + timedelta(minutes=1),
        completion_tokens=4,
        ip="8.8.8.8",
    )
    await _insert_log(
        pool,
        request_id="geo-2",
        timestamp=hour + timedelta(minutes=2),
        completion_tokens=6,
        ip="8.8.8.8",
    )
    await _insert_log(
        pool,
        request_id="geo-unknown",
        timestamp=hour + timedelta(minutes=3),
        completion_tokens=3,
        ip=None,
    )
    await _insert_log(
        pool,
        request_id="geo-synthetic",
        timestamp=hour + timedelta(minutes=4),
        completion_tokens=100,
        ip="8.8.8.8",
        synthetic=True,
    )

    assert (
        await run_rollup(
            pool,
            start=hour,
            end=hour + timedelta(hours=2),
            resolver_factory=resolver,
        )
        == 2
    )

    async with pool.acquire() as connection:
        coverage = await connection.fetch(
            """
            SELECT hour_bucket, rows_total, rows_with_ip
            FROM geo_hourly_coverage
            ORDER BY hour_bucket
            """
        )
        demand = await connection.fetch(
            """
            SELECT country_code, continent_code, request_count, completion_tokens
            FROM geo_hourly_demand
            ORDER BY country_code
            """
        )
        geo_resolver = resolver()
        try:
            payload = await aggregate_geo_demand_rollup(
                connection,
                hour,
                hour + timedelta(hours=2),
                geo_resolver,
            )
        finally:
            geo_resolver.close()

    assert [(row["rows_total"], row["rows_with_ip"]) for row in coverage] == [(3, 2), (0, 0)]
    assert [tuple(row.values()) for row in demand] == [
        ("?", "?", 1, 3),
        ("USA", "NA", 2, 10),
    ]
    assert payload is not None
    assert payload["meta"]["rows_total"] == 3
    assert payload["hours"][0]["b"] == [["USA", "NA", 2, 10], ["?", "?", 1, 3]]
    assert payload["hours"][1] == {"b": []}

    await _insert_log(
        pool,
        request_id="geo-3",
        timestamp=hour + timedelta(minutes=5),
        completion_tokens=5,
        ip="8.8.8.8",
    )
    await run_rollup(
        pool,
        start=hour,
        end=hour + timedelta(hours=1),
        resolver_factory=resolver,
    )

    async with pool.acquire() as connection:
        refreshed = await connection.fetchrow(
            """
            SELECT request_count, completion_tokens
            FROM geo_hourly_demand
            WHERE hour_bucket = $1 AND country_code = 'USA'
            """,
            hour,
        )
        raw_columns = await connection.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name IN ('geo_hourly_demand', 'geo_hourly_coverage')
            """
        )

    assert refreshed is not None
    assert (refreshed["request_count"], refreshed["completion_tokens"]) == (3, 15)
    assert "ip" not in {row["column_name"] for row in raw_columns}
