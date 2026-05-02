"""Hourly rollup of api_logs into provider_hourly_stats.

Spec: docs/superpowers/specs/2026-05-02-per-provider-hourly-performance-design.md
"""

from __future__ import annotations

import time  # noqa: F401  used in next task
from typing import TYPE_CHECKING

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from datetime import datetime, timedelta, timezone  # noqa: F401  used in next task

    import asyncpg

logger = get_logger(__name__)

# A constant 64-bit integer so all replicas serialize on the same lock.
ADVISORY_LOCK_KEY = 0x70726F76737473  # ascii "provsts" packed

ROLLUP_SQL = """
INSERT INTO provider_hourly_stats AS p (
    hour_bucket, provider, model_id,
    request_count, error_count, stream_count,
    ttft_p50_ms, ttft_p95_ms, ttft_p99_ms,
    latency_p50_ms, latency_p95_ms, latency_p99_ms,
    throughput_avg_tps, throughput_p50_tps, throughput_p95_tps,
    prompt_tokens_avg, completion_tokens_avg, total_completion_tokens
)
SELECT
    date_trunc('hour', timestamp)                                          AS hour_bucket,
    provider,
    model_id,
    COUNT(*)                                                                AS request_count,
    COUNT(*) FILTER (WHERE status_code >= 400 OR error IS NOT NULL)         AS error_count,
    COUNT(*) FILTER (WHERE stream = TRUE AND ttft_ms IS NOT NULL
                          AND status_code < 400 AND error IS NULL)          AS stream_count,

    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY ttft_ms)
        FILTER (WHERE stream = TRUE AND ttft_ms IS NOT NULL
                      AND status_code < 400 AND error IS NULL)::INT         AS ttft_p50_ms,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY ttft_ms)
        FILTER (WHERE stream = TRUE AND ttft_ms IS NOT NULL
                      AND status_code < 400 AND error IS NULL)::INT         AS ttft_p95_ms,
    PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY ttft_ms)
        FILTER (WHERE stream = TRUE AND ttft_ms IS NOT NULL
                      AND status_code < 400 AND error IS NULL)::INT         AS ttft_p99_ms,

    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY latency_ms)
        FILTER (WHERE status_code < 400 AND error IS NULL
                      AND latency_ms IS NOT NULL)::INT                      AS latency_p50_ms,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)
        FILTER (WHERE status_code < 400 AND error IS NULL
                      AND latency_ms IS NOT NULL)::INT                      AS latency_p95_ms,
    PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY latency_ms)
        FILTER (WHERE status_code < 400 AND error IS NULL
                      AND latency_ms IS NOT NULL)::INT                      AS latency_p99_ms,

    AVG(throughput_tps)                                                     AS throughput_avg_tps,
    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY throughput_tps)            AS throughput_p50_tps,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY throughput_tps)            AS throughput_p95_tps,

    AVG(prompt_tokens)::FLOAT                                               AS prompt_tokens_avg,
    AVG(completion_tokens)::FLOAT                                           AS completion_tokens_avg,
    COALESCE(SUM(completion_tokens), 0)::BIGINT                             AS total_completion_tokens
FROM (
    SELECT
        timestamp, provider, model_id, status_code, error,
        stream, ttft_ms, latency_ms, prompt_tokens, completion_tokens,
        CASE
            WHEN status_code >= 400
                 OR error IS NOT NULL
                 OR completion_tokens IS NULL
                 OR completion_tokens <= 0                  THEN NULL
            WHEN stream = TRUE AND ttft_ms IS NOT NULL
                 AND latency_ms > ttft_ms
                THEN completion_tokens::FLOAT / ((latency_ms - ttft_ms) / 1000.0)
            WHEN latency_ms > 0
                THEN completion_tokens::FLOAT / (latency_ms / 1000.0)
            ELSE NULL
        END AS throughput_tps
    FROM api_logs
    WHERE timestamp >= $1 AND timestamp < $2
) src
GROUP BY hour_bucket, provider, model_id
HAVING COUNT(*) > 0
ON CONFLICT (provider, model_id, hour_bucket) DO UPDATE SET
    request_count           = EXCLUDED.request_count,
    error_count             = EXCLUDED.error_count,
    stream_count            = EXCLUDED.stream_count,
    ttft_p50_ms             = EXCLUDED.ttft_p50_ms,
    ttft_p95_ms             = EXCLUDED.ttft_p95_ms,
    ttft_p99_ms             = EXCLUDED.ttft_p99_ms,
    latency_p50_ms          = EXCLUDED.latency_p50_ms,
    latency_p95_ms          = EXCLUDED.latency_p95_ms,
    latency_p99_ms          = EXCLUDED.latency_p99_ms,
    throughput_avg_tps      = EXCLUDED.throughput_avg_tps,
    throughput_p50_tps      = EXCLUDED.throughput_p50_tps,
    throughput_p95_tps      = EXCLUDED.throughput_p95_tps,
    prompt_tokens_avg       = EXCLUDED.prompt_tokens_avg,
    completion_tokens_avg   = EXCLUDED.completion_tokens_avg,
    total_completion_tokens = EXCLUDED.total_completion_tokens
"""


async def run_rollup(
    pool: asyncpg.Pool,
    *,
    start: datetime,
    end: datetime,
) -> int:
    """Aggregate api_logs in the half-open interval [start, end) into
    provider_hourly_stats. Returns number of rows affected (inserted+updated).

    Idempotent: re-running with the same window updates existing rows.
    Caller is responsible for taking the advisory lock when concurrent
    runs are possible.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be tz-aware")
    if end <= start:
        raise ValueError("end must be after start")

    async with pool.acquire() as conn:
        result = await conn.execute(ROLLUP_SQL, start, end)
    # asyncpg returns "INSERT 0 N" for INSERT statements (the 0 is oid).
    # Parse the trailing integer.
    try:
        return int(result.rsplit(" ", 1)[-1])
    except ValueError:
        return 0
