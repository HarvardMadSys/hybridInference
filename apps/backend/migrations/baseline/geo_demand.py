"""The country/hour demand rollup and its coverage markers, frozen.

Copied from ``serving.admin.geo_demand_rollup``.
"""

from __future__ import annotations

from typing import Any

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS geo_hourly_coverage (
        hour_bucket  TIMESTAMPTZ PRIMARY KEY,
        rows_total   BIGINT      NOT NULL CHECK (rows_total >= 0),
        rows_with_ip BIGINT      NOT NULL CHECK (
            rows_with_ip >= 0 AND rows_with_ip <= rows_total
        ),
        completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS geo_hourly_demand (
        hour_bucket      TIMESTAMPTZ NOT NULL REFERENCES geo_hourly_coverage(hour_bucket)
            ON DELETE CASCADE,
        country_code     TEXT        NOT NULL,
        continent_code   TEXT        NOT NULL,
        request_count    BIGINT      NOT NULL CHECK (request_count >= 0),
        completion_tokens BIGINT     NOT NULL,
        PRIMARY KEY (hour_bucket, country_code, continent_code)
    )
    """,
)


async def ensure_geo_rollup_schema(connection: Any) -> None:
    """Create the country rollup and explicit coverage tables idempotently."""
    for statement in _SCHEMA_STATEMENTS:
        await connection.execute(statement)
