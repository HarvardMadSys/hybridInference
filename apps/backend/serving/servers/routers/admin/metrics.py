"""Admin per-request and per-route metrics endpoints."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from serving.schemas_admin import (
    AdminClearErrorRequestsResponse,
    AdminMetricDistribution,
    AdminPerformanceMetricsResponse,
    AdminPerformanceMetricsWindow,
    AdminRecentRequestContentResponse,
    AdminRecentRequestItem,
    AdminRecentRequestsResponse,
    AdminRequestMetricsBucket,
    AdminRequestMetricsResponse,
    AdminRequestMetricsWindow,
    AdminRequestPerfBreakdownResponse,
    AdminRequestPerfDistribution,
    AdminRequestPerfGroup,
    AdminTtftScatterModel,
    AdminTtftScatterPoint,
    AdminTtftScatterResponse,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import (
    get_db_logger,
    get_log_store,
    get_operational_store,
    verify_admin_access,
)
from serving.servers.routers.admin._common import (
    _build_histogram,
    _escape_ilike_substring_term,
    _round_or_none,
)
from serving.storage.utils import coerce_json_object
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/admin")


REQUEST_METRIC_WINDOWS: tuple[tuple[str, str, int, int], ...] = (
    ("1h", "Last 1 hour", 60, 5),
    ("1d", "Last 1 day", 1440, 60),
    ("1w", "Last 1 week", 10080, 360),
    ("1mo", "Last 1 month", 43200, 1440),
)


# Inner edges for histogram buckets, passed to width_bucket(x, ARRAY[...]).
# With N = len(edges), width_bucket returns bucket 0 for x < edges[0]
# (underflow; ignored here because these metrics are never negative),
# buckets 1..N-1 for bounded ranges [edges[k-1], edges[k]), and bucket N
# for the final open-ended overflow bucket [edges[-1], +inf).
_TOKEN_HISTOGRAM_EDGES: tuple[float, ...] = (0, 32, 128, 512, 2048, 8192, 32768, 131072)
_LATENCY_HISTOGRAM_EDGES: tuple[float, ...] = (0, 50, 100, 250, 500, 1000, 2500, 5000, 10000)
_THROUGHPUT_HISTOGRAM_EDGES: tuple[float, ...] = (0, 5, 10, 25, 50, 100, 250, 500, 1000)

# The dashboard can mount this section multiple times in a short period (for
# example while switching admin tabs).  Exact percentiles over a month's log
# history are deliberately expensive, and the underlying data does not need
# sub-second freshness.  Keep one short, per-process result cache and
# coalesce concurrent misses so a tab switch cannot multiply that work.
_PERFORMANCE_METRICS_CACHE_TTL_SECONDS = 30.0
_PERFORMANCE_METRICS_CACHE: tuple[float, int, AdminPerformanceMetricsResponse] | None = None
_PERFORMANCE_METRICS_LOCK: asyncio.Lock | None = None
_PERFORMANCE_METRICS_LOCK_LOOP: asyncio.AbstractEventLoop | None = None


def _performance_metrics_lock() -> asyncio.Lock:
    """Return a lock bound to the active event loop for metric-cache misses."""
    global _PERFORMANCE_METRICS_LOCK, _PERFORMANCE_METRICS_LOCK_LOOP

    loop = asyncio.get_running_loop()
    if _PERFORMANCE_METRICS_LOCK is None or _PERFORMANCE_METRICS_LOCK_LOOP is not loop:
        _PERFORMANCE_METRICS_LOCK = asyncio.Lock()
        _PERFORMANCE_METRICS_LOCK_LOOP = loop
    return _PERFORMANCE_METRICS_LOCK


def _distribution_from_row(
    row: Any,
    prefix: str,
    edges: tuple[float, ...],
    bucket_counts: dict[int, int],
) -> AdminMetricDistribution:
    """Build a distribution from a stats row + histogram counts dict."""
    percentiles = row[f"{prefix}_percentiles"] or ()
    p50, p90, p95, p99 = (*percentiles, None, None, None, None)[:4]
    return AdminMetricDistribution(
        count=int(row[f"{prefix}_count"] or 0),
        mean=_round_or_none(row[f"{prefix}_mean"]),
        min=_round_or_none(row[f"{prefix}_min"]),
        max=_round_or_none(row[f"{prefix}_max"]),
        p50=_round_or_none(p50),
        p90=_round_or_none(p90),
        p95=_round_or_none(p95),
        p99=_round_or_none(p99),
        histogram=_build_histogram(edges, bucket_counts),
    )


# Shared CTE prefix used by both the stats query and the histogram query for
# each window. Defining it once keeps the row filter / throughput derivation in sync
# so the "histogram sums to count" invariant cannot drift.
_PERF_METRICS_CTE = """
WITH base AS (
    SELECT
        prompt_tokens,
        completion_tokens,
        ttft_ms,
        latency_ms,
        stream
    FROM api_logs
    WHERE timestamp >= NOW() - ($1::int * interval '1 minute')
      AND status_code BETWEEN 200 AND 399
      -- Embeddings have a fundamentally different latency/token profile and no
      -- completion tokens; exclude them so they don't skew chat-perf percentiles.
      AND (metadata->>'request_type') IS DISTINCT FROM 'embedding'
),
derived AS (
    SELECT
        prompt_tokens,
        completion_tokens,
        CASE
            WHEN stream = TRUE AND ttft_ms IS NOT NULL
                THEN ttft_ms::float
        END AS ttft_ms,
        CASE
            WHEN stream = TRUE
                AND ttft_ms IS NOT NULL
                AND completion_tokens IS NOT NULL
                AND completion_tokens > 1
                AND latency_ms IS NOT NULL
                AND latency_ms > ttft_ms
                THEN (completion_tokens - 1)::float * 1000.0
                     / NULLIF(latency_ms - ttft_ms, 0)
        END AS throughput_tps
    FROM base
)
"""


_DECODE_MIN_WINDOW_MS = 2000
_DECODE_MIN_TOKENS = 8


def _decode_throughput_tps(
    stream: bool | None,
    latency_ms: int | None,
    ttft_ms: int | None,
    completion_tokens: int | None,
) -> float | None:
    """Output-token throughput (tok/s) over the decode phase, or None if undefined.

    Matches the convention used by /admin/performance-metrics: first token is
    delivered at ttft_ms, so the decode phase produces (completion_tokens - 1)
    tokens during (latency_ms - ttft_ms). Streaming-only; needs >1 output token.

    Additionally returns None when the decode window is shorter than
    `_DECODE_MIN_WINDOW_MS` (2000 ms) or fewer than `_DECODE_MIN_TOKENS` (8)
    completion tokens were produced. Sub-2-second decode windows and very short
    streams produce noise-dominated throughput numbers (e.g. ~100k tok/s) when
    upstream SSE is buffered or the response collapses to ~0-1 ms of decode
    time, so we render those rows as undefined rather than displaying
    physically implausible values.
    """
    if stream is not True:
        return None
    if ttft_ms is None or ttft_ms <= 0:
        return None
    if latency_ms is None or latency_ms <= ttft_ms:
        return None
    if completion_tokens is None or completion_tokens <= 1:
        return None
    if latency_ms - ttft_ms < _DECODE_MIN_WINDOW_MS:
        return None
    if completion_tokens < _DECODE_MIN_TOKENS:
        return None
    return (completion_tokens - 1) / ((latency_ms - ttft_ms) / 1000.0)


# SQL twin of :func:`_decode_throughput_tps` over an ``api_logs l`` row, for
# aggregate queries that must agree with the per-row Decode column. The
# thresholds are interpolated from the same constants so the two can't drift.
# The streaming guard is left to the caller's row filter (``l.stream = TRUE``),
# and ``_DECODE_MIN_TOKENS`` (> 1) subsumes the helper's "more than one output
# token" check.
_DECODE_THROUGHPUT_SQL = f"""
                    CASE
                        WHEN l.ttft_ms IS NOT NULL AND l.ttft_ms > 0
                            AND l.latency_ms IS NOT NULL
                            AND l.latency_ms - l.ttft_ms >= {_DECODE_MIN_WINDOW_MS}
                            AND l.completion_tokens IS NOT NULL
                            AND l.completion_tokens >= {_DECODE_MIN_TOKENS}
                            THEN (l.completion_tokens - 1)::float * 1000.0
                                 / (l.latency_ms - l.ttft_ms)
                    END""".strip()

# Cap on (model, endpoint) pairs returned by the per-route summary. Well above
# any realistic route count, so it only guards against a pathological spread of
# historical endpoint labels blowing up the payload.
_PERF_BREAKDOWN_MAX_GROUPS = 100

# The per-route summary computes exact percentiles over every matching row, and
# the group cap above applies only after aggregation, so it saves no scan work.
# The panel behind it refetches on each settled filter change and on Refresh,
# and an admin can pick a 90-day lookback. Cache per filter tuple for a few
# seconds and serialize misses, so a burst of filter edits — or a second admin
# on the same tab — cannot pile overlapping month-scale scans onto the pool.
# Same reasoning as _PERFORMANCE_METRICS_CACHE; keyed because this view is
# parameterized rather than fixed-window. Queued misses for *different* filters
# still run once the lock frees (the query cannot be cancelled server-side),
# but at most one runs at a time.
_PERF_BREAKDOWN_CACHE_TTL_SECONDS = 20.0
# Bound on distinct filter tuples kept. A session of filter edits walks through
# many keys, and each entry holds only a small summary.
_PERF_BREAKDOWN_CACHE_MAX_ENTRIES = 32
# (pool id, days, user_id, model_id, request_type)
_PerfBreakdownKey = tuple[int, int, str | None, str | None, str | None]
_PERF_BREAKDOWN_CACHE: dict[_PerfBreakdownKey, tuple[float, AdminRequestPerfBreakdownResponse]] = {}
_PERF_BREAKDOWN_LOCK: asyncio.Lock | None = None
_PERF_BREAKDOWN_LOCK_LOOP: asyncio.AbstractEventLoop | None = None


def _perf_breakdown_lock() -> asyncio.Lock:
    """Return a lock bound to the active event loop for breakdown cache misses."""
    global _PERF_BREAKDOWN_LOCK, _PERF_BREAKDOWN_LOCK_LOOP

    loop = asyncio.get_running_loop()
    if _PERF_BREAKDOWN_LOCK is None or _PERF_BREAKDOWN_LOCK_LOOP is not loop:
        _PERF_BREAKDOWN_LOCK = asyncio.Lock()
        _PERF_BREAKDOWN_LOCK_LOOP = loop
    return _PERF_BREAKDOWN_LOCK


def _perf_breakdown_cached(
    key: _PerfBreakdownKey,
) -> AdminRequestPerfBreakdownResponse | None:
    """Return a still-fresh cached breakdown for *key*, dropping it once stale."""
    entry = _PERF_BREAKDOWN_CACHE.get(key)
    if entry is None:
        return None
    cached_at, response = entry
    if time.monotonic() - cached_at >= _PERF_BREAKDOWN_CACHE_TTL_SECONDS:
        _PERF_BREAKDOWN_CACHE.pop(key, None)
        return None
    return response


def _perf_breakdown_store(
    key: _PerfBreakdownKey,
    response: AdminRequestPerfBreakdownResponse,
) -> None:
    """Cache *response* under *key*, evicting the oldest entries past the cap."""
    # Re-insert rather than assign so the key moves to the end: dicts preserve
    # insertion order, which is what makes "pop the first key" evict the oldest.
    _PERF_BREAKDOWN_CACHE.pop(key, None)
    _PERF_BREAKDOWN_CACHE[key] = (time.monotonic(), response)
    while len(_PERF_BREAKDOWN_CACHE) > _PERF_BREAKDOWN_CACHE_MAX_ENTRIES:
        _PERF_BREAKDOWN_CACHE.pop(next(iter(_PERF_BREAKDOWN_CACHE)))


def _perf_distribution_from_row(row: Any, prefix: str) -> AdminRequestPerfDistribution:
    """Build a mean/P10/median/P90 summary from a ``{prefix}_*`` aggregate row."""
    percentiles = row[f"{prefix}_percentiles"] or ()
    p10, p50, p90 = (*percentiles, None, None, None)[:3]
    return AdminRequestPerfDistribution(
        count=int(row[f"{prefix}_count"] or 0),
        mean=_round_or_none(row[f"{prefix}_mean"]),
        p10=_round_or_none(p10),
        p50=_round_or_none(p50),
        p90=_round_or_none(p90),
    )


@router.get("/request-metrics", response_model=AdminRequestMetricsResponse)
async def admin_get_request_metrics(
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> AdminRequestMetricsResponse:
    """Return request count trends for admin dashboard lookback windows."""
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    windows: list[AdminRequestMetricsWindow] = []
    async with db_logger.pool.acquire() as conn:
        for key, label, window_minutes, bucket_minutes in REQUEST_METRIC_WINDOWS:
            rows = await conn.fetch(
                """
                WITH config AS (
                    SELECT ($2::int * 60) AS bucket_seconds
                ),
                bounds AS (
                    SELECT
                        date_trunc('minute', NOW()) AS end_time,
                        date_trunc('minute', NOW())
                            - ($1::int * interval '1 minute') AS start_time,
                        to_timestamp(
                            floor(
                                extract(
                                    epoch FROM date_trunc('minute', NOW())
                                        - ($1::int * interval '1 minute')
                                ) / config.bucket_seconds
                            ) * config.bucket_seconds
                        ) AS aligned_start
                    FROM config
                ),
                series AS (
                    SELECT generate_series(
                        (SELECT aligned_start FROM bounds),
                        (SELECT end_time FROM bounds),
                        $2::int * interval '1 minute'
                    ) AS bucket_start
                ),
                bucketed_logs AS (
                    SELECT
                        to_timestamp(
                            floor(extract(epoch from timestamp) / ($2::int * 60))
                            * ($2::int * 60)
                        ) AS bucket_start,
                        COUNT(*) AS request_count,
                        COUNT(*) FILTER (
                            WHERE status_code >= 200 AND status_code < 400
                        ) AS success_count,
                        COUNT(*) FILTER (
                            WHERE error IS NOT NULL
                               OR status_code IS NULL
                               OR status_code < 200
                               OR status_code >= 400
                        ) AS error_count,
                        COUNT(latency_ms) FILTER (WHERE latency_ms IS NOT NULL)
                            AS latency_count,
                        SUM(latency_ms) FILTER (WHERE latency_ms IS NOT NULL)
                            AS latency_sum_ms,
                        AVG(latency_ms) FILTER (WHERE latency_ms IS NOT NULL)
                            AS avg_latency_ms
                    FROM api_logs, bounds
                    WHERE timestamp >= bounds.start_time
                      AND timestamp <= bounds.end_time
                    GROUP BY 1
                )
                SELECT
                    series.bucket_start,
                    COALESCE(bucketed_logs.request_count, 0) AS request_count,
                    COALESCE(bucketed_logs.success_count, 0) AS success_count,
                    COALESCE(bucketed_logs.error_count, 0) AS error_count,
                    COALESCE(bucketed_logs.latency_count, 0) AS latency_count,
                    COALESCE(bucketed_logs.latency_sum_ms, 0) AS latency_sum_ms,
                    bucketed_logs.avg_latency_ms
                FROM series
                LEFT JOIN bucketed_logs
                  ON bucketed_logs.bucket_start = series.bucket_start
                ORDER BY series.bucket_start ASC
                """,
                window_minutes,
                bucket_minutes,
            )

            buckets = [
                AdminRequestMetricsBucket(
                    start_time=row["bucket_start"],
                    request_count=int(row["request_count"] or 0),
                    success_count=int(row["success_count"] or 0),
                    error_count=int(row["error_count"] or 0),
                    avg_latency_ms=(
                        round(float(row["avg_latency_ms"]), 1)
                        if row["avg_latency_ms"] is not None
                        else None
                    ),
                )
                for row in rows
            ]
            total_requests = sum(bucket.request_count for bucket in buckets)
            success_requests = sum(bucket.success_count for bucket in buckets)
            error_requests = sum(bucket.error_count for bucket in buckets)
            latency_count = sum(int(row["latency_count"] or 0) for row in rows)
            latency_sum_ms = sum(float(row["latency_sum_ms"] or 0) for row in rows)
            windows.append(
                AdminRequestMetricsWindow(
                    key=key,
                    label=label,
                    window_minutes=window_minutes,
                    bucket_minutes=bucket_minutes,
                    total_requests=total_requests,
                    success_requests=success_requests,
                    error_requests=error_requests,
                    avg_latency_ms=(
                        round(latency_sum_ms / latency_count, 1) if latency_count else None
                    ),
                    buckets=buckets,
                )
            )

    return AdminRequestMetricsResponse(
        generated_at=datetime.now(timezone.utc),
        windows=windows,
    )


async def _load_performance_metrics(db_logger) -> AdminPerformanceMetricsResponse:
    """Return prompt/response length and latency distributions per lookback window.

    For each window, computes percentiles (p50/p90/p95/p99), mean/min/max, count,
    and a small histogram for:

    - prompt_tokens (over rows where prompt_tokens > 0)
    - completion_tokens (over rows where completion_tokens > 0)
    - ttft_ms (over streaming rows with ttft_ms NOT NULL)
    - throughput_tps (over streaming rows with completion_tokens > 1, derived from
      (completion_tokens - 1) * 1000 / (latency_ms - ttft_ms); requires
      latency_ms > ttft_ms — non-positive decode time clamped to NULL)

    Only successful requests (status_code 200-399) are included.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    token_edges = list(_TOKEN_HISTOGRAM_EDGES)
    latency_edges = list(_LATENCY_HISTOGRAM_EDGES)
    throughput_edges = list(_THROUGHPUT_HISTOGRAM_EDGES)

    windows: list[AdminPerformanceMetricsWindow] = []
    async with db_logger.pool.acquire() as conn:
        for key, label, window_minutes, _bucket_minutes in REQUEST_METRIC_WINDOWS:
            # Aggregate stats — one row, one query per window.
            stats_row = await conn.fetchrow(
                _PERF_METRICS_CTE
                + """
                SELECT
                    COUNT(*) FILTER (
                        WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0
                    ) AS pt_count,
                    AVG(prompt_tokens) FILTER (
                        WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0
                    ) AS pt_mean,
                    MIN(prompt_tokens) FILTER (
                        WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0
                    ) AS pt_min,
                    MAX(prompt_tokens) FILTER (
                        WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0
                    ) AS pt_max,
                    percentile_cont(ARRAY[0.5, 0.9, 0.95, 0.99]::float8[])
                        WITHIN GROUP (ORDER BY prompt_tokens)
                        FILTER (WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0)
                        AS pt_percentiles,

                    COUNT(*) FILTER (
                        WHERE completion_tokens IS NOT NULL AND completion_tokens > 0
                    ) AS ct_count,
                    AVG(completion_tokens) FILTER (
                        WHERE completion_tokens IS NOT NULL AND completion_tokens > 0
                    ) AS ct_mean,
                    MIN(completion_tokens) FILTER (
                        WHERE completion_tokens IS NOT NULL AND completion_tokens > 0
                    ) AS ct_min,
                    MAX(completion_tokens) FILTER (
                        WHERE completion_tokens IS NOT NULL AND completion_tokens > 0
                    ) AS ct_max,
                    percentile_cont(ARRAY[0.5, 0.9, 0.95, 0.99]::float8[])
                        WITHIN GROUP (ORDER BY completion_tokens)
                        FILTER (WHERE completion_tokens IS NOT NULL AND completion_tokens > 0)
                        AS ct_percentiles,

                    COUNT(*) FILTER (WHERE ttft_ms IS NOT NULL) AS tt_count,
                    AVG(ttft_ms) FILTER (WHERE ttft_ms IS NOT NULL) AS tt_mean,
                    MIN(ttft_ms) FILTER (WHERE ttft_ms IS NOT NULL) AS tt_min,
                    MAX(ttft_ms) FILTER (WHERE ttft_ms IS NOT NULL) AS tt_max,
                    percentile_cont(ARRAY[0.5, 0.9, 0.95, 0.99]::float8[])
                        WITHIN GROUP (ORDER BY ttft_ms)
                        FILTER (WHERE ttft_ms IS NOT NULL) AS tt_percentiles,

                    COUNT(*) FILTER (WHERE throughput_tps IS NOT NULL) AS tp_count,
                    AVG(throughput_tps) FILTER (WHERE throughput_tps IS NOT NULL) AS tp_mean,
                    MIN(throughput_tps) FILTER (WHERE throughput_tps IS NOT NULL) AS tp_min,
                    MAX(throughput_tps) FILTER (WHERE throughput_tps IS NOT NULL) AS tp_max,
                    percentile_cont(ARRAY[0.5, 0.9, 0.95, 0.99]::float8[])
                        WITHIN GROUP (ORDER BY throughput_tps)
                        FILTER (WHERE throughput_tps IS NOT NULL) AS tp_percentiles
                FROM derived
                """,
                window_minutes,
            )

            # Histogram counts — one row per (metric, bucket).
            hist_rows = await conn.fetch(
                _PERF_METRICS_CTE
                + """
                SELECT 'prompt_tokens' AS metric,
                       width_bucket(prompt_tokens::float, $2::float[]) AS bucket,
                       COUNT(*) AS cnt
                FROM derived
                WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0
                GROUP BY bucket
                UNION ALL
                SELECT 'completion_tokens' AS metric,
                       width_bucket(completion_tokens::float, $2::float[]) AS bucket,
                       COUNT(*) AS cnt
                FROM derived
                WHERE completion_tokens IS NOT NULL AND completion_tokens > 0
                GROUP BY bucket
                UNION ALL
                SELECT 'ttft_ms' AS metric,
                       width_bucket(ttft_ms, $3::float[]) AS bucket,
                       COUNT(*) AS cnt
                FROM derived
                WHERE ttft_ms IS NOT NULL
                GROUP BY bucket
                UNION ALL
                SELECT 'throughput_tps' AS metric,
                       width_bucket(throughput_tps, $4::float[]) AS bucket,
                       COUNT(*) AS cnt
                FROM derived
                WHERE throughput_tps IS NOT NULL
                GROUP BY bucket
                """,
                window_minutes,
                token_edges,
                latency_edges,
                throughput_edges,
            )

            histograms: dict[str, dict[int, int]] = {
                "prompt_tokens": {},
                "completion_tokens": {},
                "ttft_ms": {},
                "throughput_tps": {},
            }
            for row in hist_rows:
                metric = row["metric"]
                bucket = int(row["bucket"] or 0)
                cnt = int(row["cnt"] or 0)
                # width_bucket can return 0 for negative values; for token
                # metrics this can't happen (filtered to > 0), but for throughput_tps
                # we already clamped. Fold any underflow into bucket 1 just
                # in case so that sum(histogram) == count holds.
                target = bucket if bucket >= 1 else 1
                histograms[metric][target] = histograms[metric].get(target, 0) + cnt

            # `stats_row` is always non-None: an aggregate SELECT without
            # GROUP BY returns exactly one row even when `derived` is empty.
            window = AdminPerformanceMetricsWindow(
                key=key,
                label=label,
                window_minutes=window_minutes,
                prompt_tokens=_distribution_from_row(
                    stats_row, "pt", _TOKEN_HISTOGRAM_EDGES, histograms["prompt_tokens"]
                ),
                completion_tokens=_distribution_from_row(
                    stats_row, "ct", _TOKEN_HISTOGRAM_EDGES, histograms["completion_tokens"]
                ),
                ttft_ms=_distribution_from_row(
                    stats_row, "tt", _LATENCY_HISTOGRAM_EDGES, histograms["ttft_ms"]
                ),
                throughput_tps=_distribution_from_row(
                    stats_row, "tp", _THROUGHPUT_HISTOGRAM_EDGES, histograms["throughput_tps"]
                ),
            )
            windows.append(window)

    return AdminPerformanceMetricsResponse(
        generated_at=datetime.now(timezone.utc),
        windows=windows,
    )


async def _get_cached_performance_metrics(
    db_logger,
    *,
    refresh: bool = False,
) -> AdminPerformanceMetricsResponse:
    """Load performance metrics, reusing a short-lived result when possible."""
    global _PERFORMANCE_METRICS_CACHE

    pool_id = id(db_logger.pool)
    now = time.monotonic()
    cached = _PERFORMANCE_METRICS_CACHE
    if (
        not refresh
        and cached is not None
        and cached[1] == pool_id
        and now - cached[0] < _PERFORMANCE_METRICS_CACHE_TTL_SECONDS
    ):
        return cached[2]

    # Serialize cache misses. The inner check means simultaneous dashboard
    # mounts all reuse the one query instead of each executing eight scans.
    async with _performance_metrics_lock():
        now = time.monotonic()
        cached = _PERFORMANCE_METRICS_CACHE
        if (
            not refresh
            and cached is not None
            and cached[1] == pool_id
            and now - cached[0] < _PERFORMANCE_METRICS_CACHE_TTL_SECONDS
        ):
            return cached[2]

        metrics = await _load_performance_metrics(db_logger)
        _PERFORMANCE_METRICS_CACHE = (time.monotonic(), pool_id, metrics)
        return metrics


@router.get("/performance-metrics", response_model=AdminPerformanceMetricsResponse)
async def admin_get_performance_metrics(
    refresh: bool = False,
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> AdminPerformanceMetricsResponse:
    """Return cached performance distributions; ``refresh`` bypasses the cache."""
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")
    return await _get_cached_performance_metrics(db_logger, refresh=refresh)


@router.get("/ttft-scatter", response_model=AdminTtftScatterResponse)
async def admin_get_ttft_scatter(
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> AdminTtftScatterResponse:
    """Return TTFT vs input length scatter data per (model, provider).

    For each (model_id, provider) pair, returns up to the last 1000 successful
    streaming requests *per cache-hit class* (so up to 1000 cached and 1000
    uncached) with a recorded TTFT and a non-empty prompt. Partitioning by
    cache class keeps the rarer uncached series well-populated instead of
    being crowded out by cache hits. The query is bounded to the last 90 days
    so the `ROW_NUMBER()` scan stays bounded as `api_logs` grows. `cache_hit`
    is true iff `cache_read_tokens > 0`.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    async with db_logger.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH ranked AS (
                SELECT
                    model_id,
                    provider,
                    prompt_tokens,
                    ttft_ms,
                    cache_read_tokens,
                    timestamp,
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            model_id, provider, (COALESCE(cache_read_tokens, 0) > 0)
                        ORDER BY timestamp DESC
                    ) AS rn
                FROM api_logs
                WHERE timestamp >= NOW() - INTERVAL '90 days'
                  AND ttft_ms IS NOT NULL
                  AND prompt_tokens IS NOT NULL
                  AND prompt_tokens > 0
                  AND status_code BETWEEN 200 AND 399
                  AND stream = TRUE
            )
            SELECT model_id, provider, prompt_tokens, ttft_ms,
                   cache_read_tokens, timestamp
            FROM ranked
            WHERE rn <= 1000
            ORDER BY model_id, provider, timestamp DESC
            """
        )

    by_pair: dict[tuple[str, str], list[AdminTtftScatterPoint]] = {}
    for row in rows:
        key = (row["model_id"], row["provider"])
        cache_read = row["cache_read_tokens"]
        point = AdminTtftScatterPoint(
            prompt_tokens=int(row["prompt_tokens"]),
            ttft_ms=int(row["ttft_ms"]),
            cache_hit=cache_read is not None and cache_read > 0,
            timestamp=row["timestamp"],
        )
        by_pair.setdefault(key, []).append(point)

    models = [
        AdminTtftScatterModel(model_id=model_id, provider=provider, points=points)
        for (model_id, provider), points in by_pair.items()
    ]
    models.sort(key=lambda m: len(m.points), reverse=True)

    return AdminTtftScatterResponse(models=models)


def _build_recent_requests_filters(
    *,
    days: int,
    user_id: str | None = None,
    model_id: str | None = None,
    status_code: int | None = None,
    errors_only: bool = False,
    request_type: str | None = None,
) -> tuple[list[str], list[Any], bool]:
    """Build the WHERE clauses + bind params shared by the Recent Requests views.

    Returns ``(clauses, params, needs_user_join)``. Placeholders are numbered
    from ``$1`` in the returned param order, so a caller can append its own
    params (LIMIT/OFFSET, a group cap) after these. ``needs_user_join`` is True
    when a clause references the joined ``users`` row — api_logs is high-volume,
    so callers skip that join whenever no predicate needs it.

    The list view and the per-route performance summary run over the same rows,
    so both build their filters here: a predicate added for one can't silently
    leave the other summarizing a different slice of traffic.
    """
    # Always bound by the lookback window so no query scans the full retention
    # range.
    clauses: list[str] = []
    params: list[Any] = [days]
    clauses.append(f"l.timestamp >= NOW() - make_interval(days => ${len(params)}::int)")
    needs_user_join = False

    if user_id:
        # Substring match across the user id and the joined user's name/email so
        # admins can search by any of the identifiers shown in the table, not
        # just an exact user id. Matching runs server-side across the full
        # lookback window, so it isn't limited to the current page of results.
        params.append(_escape_ilike_substring_term(user_id))
        idx = len(params)
        clauses.append(
            f"(l.user_id ILIKE '%' || ${idx} || '%' ESCAPE '\\' "
            f"OR u.user_name ILIKE '%' || ${idx} || '%' ESCAPE '\\' "
            f"OR u.email ILIKE '%' || ${idx} || '%' ESCAPE '\\')"
        )
        needs_user_join = True

    if model_id:
        params.append(_escape_ilike_substring_term(model_id))
        clauses.append(f"l.model_id ILIKE '%' || ${len(params)} || '%' ESCAPE '\\'")

    if status_code is not None:
        clauses.append(f"l.status_code = ${len(params) + 1}")
        params.append(status_code)

    if errors_only:
        clauses.append(
            "(l.error IS NOT NULL OR l.status_code IS NULL "
            "OR l.status_code < 200 OR l.status_code >= 400)"
        )

    # Optional request-type filter so admins can isolate embedding traffic
    # (tagged ``metadata.request_type = "embedding"``) from chat/completions,
    # which carry no such tag. Embedding rows are interleaved with much
    # higher-volume chat traffic and ordered by time, so without this filter
    # they are easily pushed past the first page — the reason they looked
    # "missing" from the admin dashboard while still visible in a user's own
    # (low-volume) Recent Requests view. Bound as a constant predicate (no new
    # parameter) so the LIMIT/OFFSET placeholder indices stay correct.
    if request_type == "embedding":
        clauses.append("(l.metadata->>'request_type') = 'embedding'")
    elif request_type == "chat":
        clauses.append("(l.metadata->>'request_type') IS DISTINCT FROM 'embedding'")

    return clauses, params, needs_user_join


@router.get("/recent-requests", response_model=AdminRecentRequestsResponse)
async def admin_list_recent_requests(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    days: int = 7,
    user_id: str | None = None,
    model_id: str | None = None,
    status_code: int | None = None,
    errors_only: bool = False,
    request_type: str | None = None,
    admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> AdminRecentRequestsResponse:
    """List recent API requests across all users.

    Query Parameters:
    - limit: Max results (default: 50, max: 200)
    - offset: Pagination offset
    - days: Lookback window in days (default: 7, clamped to [1, 90])
    - user_id: Filter by user ID, name, or email (substring match)
    - model_id: Filter by model ID
    - status_code: Filter by HTTP status code
    - errors_only: If true, only show requests with errors
    - request_type: ``"embedding"`` to show only embedding requests, ``"chat"``
      to exclude them; any other value (or omission) applies no type filter

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    days = max(1, min(days, 90))

    where_clauses, params, needs_user_join = _build_recent_requests_filters(
        days=days,
        user_id=user_id,
        model_id=model_id,
        status_code=status_code,
        errors_only=errors_only,
        request_type=request_type,
    )
    where_sql = "WHERE " + " AND ".join(where_clauses)

    async with db_logger.pool.acquire() as conn:
        # Get total count. Only join users when a filter needs it —
        # u.user_name/u.email are only referenced by the user predicate, and
        # api_logs is high-volume so the join is worth avoiding otherwise.
        count_join_sql = "LEFT JOIN users u ON u.id = l.user_id " if needs_user_join else ""
        count_row = await conn.fetchrow(
            f"SELECT COUNT(*) as total FROM api_logs l {count_join_sql}{where_sql}",
            *params,
        )
        total = int(count_row["total"] or 0) if count_row else 0

        # Get paginated results. prompt/response are deliberately excluded —
        # they are fetched on-demand via /admin/recent-requests/{id}/content
        # when the admin expands a row.
        limit_idx = len(params) + 1
        offset_idx = len(params) + 2
        rows = await conn.fetch(
            f"""
            SELECT
                l.request_id, l.user_id, u.user_name, u.email AS user_email,
                l.model_id, l.provider, l.timestamp,
                l.status_code, l.latency_ms, l.ttft_ms, l.stream,
                l.prompt_tokens, l.completion_tokens, l.reasoning_tokens,
                l.cache_read_tokens, l.cache_write_tokens,
                l.total_tokens, l.cost_usd, l.error,
                l.metadata->>'ip' AS user_ip,
                l.metadata->>'peer_ip' AS peer_ip,
                l.metadata->>'ip_source' AS ip_source,
                l.metadata->>'x_forwarded_for' AS x_forwarded_for,
                l.metadata->>'user_agent' AS user_agent,
                l.metadata->>'referer' AS referer,
                l.metadata->>'agent' AS agent,
                l.metadata->>'session_id' AS session_id,
                l.metadata->>'surface' AS request_surface,
                l.metadata->>'request_type' AS request_type,
                l.metadata->'routewise' AS routewise,
                l.num_turns, l.num_user_turns, l.num_tool_calls
            FROM api_logs l
            LEFT JOIN users u ON u.id = l.user_id
            {where_sql}
            ORDER BY l.timestamp DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
            """,
            *params,
            limit,
            offset,
        )

    requests = [
        AdminRecentRequestItem(
            request_id=row["request_id"],
            user_id=row["user_id"],
            user_name=row["user_name"],
            user_email=row["user_email"],
            user_ip=row["user_ip"],
            peer_ip=row["peer_ip"],
            ip_source=row["ip_source"],
            x_forwarded_for=row["x_forwarded_for"],
            user_agent=row["user_agent"],
            referer=row["referer"],
            agent=row["agent"],
            session_id=row["session_id"],
            request_surface=row["request_surface"],
            model_id=row["model_id"],
            provider=row["provider"],
            timestamp=row["timestamp"],
            status_code=row["status_code"],
            latency_ms=row["latency_ms"],
            ttft_ms=row["ttft_ms"],
            decode_throughput_tps=_decode_throughput_tps(
                row["stream"],
                row["latency_ms"],
                row["ttft_ms"],
                row["completion_tokens"],
            ),
            stream=row["stream"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            reasoning_tokens=row["reasoning_tokens"],
            cache_read_tokens=row["cache_read_tokens"],
            cache_write_tokens=row["cache_write_tokens"],
            total_tokens=row["total_tokens"],
            cost_usd=float(row["cost_usd"]) if row["cost_usd"] is not None else None,
            error=row["error"],
            routewise=coerce_json_object(row.get("routewise")),
            request_type=row.get("request_type"),
            num_turns=row.get("num_turns"),
            num_user_turns=row.get("num_user_turns"),
            num_tool_calls=row.get("num_tool_calls"),
        )
        for row in rows
    ]

    return AdminRecentRequestsResponse(requests=requests, total=total, limit=limit, offset=offset)


async def _load_request_perf_breakdown(
    db_logger,
    *,
    days: int,
    user_id: str | None,
    model_id: str | None,
    request_type: str | None,
) -> AdminRequestPerfBreakdownResponse:
    """Aggregate TTFT and decode throughput per served (model, endpoint) pair.

    One grouped scan over the filtered window. Callers go through
    :func:`_get_cached_request_perf_breakdown` so a burst of filter changes does
    not run this repeatedly; ``days`` is expected pre-clamped by the handler.

    Rows are restricted to *successful streaming* requests
    (``status_code`` 200-399, ``stream = TRUE``): a failed request has no
    meaningful latency profile, and TTFT on a non-streamed response is just its
    total latency, which would drag the percentiles toward whole-response time.
    Throughput uses the same guardrails as the table's per-row Decode column
    (see :func:`_decode_throughput_tps`), so a row's value and this summary
    can be reconciled. Embedding traffic never streams, so it drops out here
    regardless of ``request_type``.
    """
    where_clauses, params, needs_user_join = _build_recent_requests_filters(
        days=days,
        user_id=user_id,
        model_id=model_id,
        request_type=request_type,
    )
    where_clauses.append("l.status_code BETWEEN 200 AND 399")
    where_clauses.append("l.stream = TRUE")
    where_sql = "WHERE " + " AND ".join(where_clauses)
    join_sql = "LEFT JOIN users u ON u.id = l.user_id " if needs_user_join else ""
    # Fetch one extra group so a capped result can be reported as truncated
    # rather than silently passing for the whole picture. Postgres placeholders
    # are 1-based, so the cap binds as $(len(params) + 1) — appended after the
    # filter params the helper already numbered.
    group_limit_idx = len(params) + 1
    params.append(_PERF_BREAKDOWN_MAX_GROUPS + 1)

    async with db_logger.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            WITH base AS (
                SELECT
                    COALESCE(NULLIF(l.served_model_id, ''), l.model_id) AS served_model,
                    COALESCE(NULLIF(l.served_endpoint_id, ''), l.provider) AS served_endpoint,
                    -- ttft_ms > 0 (not merely NOT NULL) so both metrics agree on
                    -- what counts as a measured first token, as the row-level
                    -- helper does.
                    CASE
                        WHEN l.ttft_ms IS NOT NULL AND l.ttft_ms > 0
                            THEN l.ttft_ms::float
                    END AS ttft_ms,
                    {_DECODE_THROUGHPUT_SQL} AS throughput_tps
                FROM api_logs l
                {join_sql}{where_sql}
            )
            SELECT
                served_model,
                served_endpoint,
                COUNT(*) AS request_count,

                COUNT(ttft_ms) AS tt_count,
                AVG(ttft_ms) AS tt_mean,
                percentile_cont(ARRAY[0.1, 0.5, 0.9]::float8[])
                    WITHIN GROUP (ORDER BY ttft_ms)
                    FILTER (WHERE ttft_ms IS NOT NULL) AS tt_percentiles,

                COUNT(throughput_tps) AS tp_count,
                AVG(throughput_tps) AS tp_mean,
                percentile_cont(ARRAY[0.1, 0.5, 0.9]::float8[])
                    WITHIN GROUP (ORDER BY throughput_tps)
                    FILTER (WHERE throughput_tps IS NOT NULL) AS tp_percentiles
            FROM base
            GROUP BY served_model, served_endpoint
            ORDER BY request_count DESC, served_model ASC, served_endpoint ASC
            LIMIT ${group_limit_idx}
            """,
            *params,
        )

    truncated = len(rows) > _PERF_BREAKDOWN_MAX_GROUPS
    groups = [
        AdminRequestPerfGroup(
            model_id=row["served_model"],
            endpoint_id=row["served_endpoint"],
            request_count=int(row["request_count"] or 0),
            ttft_ms=_perf_distribution_from_row(row, "tt"),
            decode_throughput_tps=_perf_distribution_from_row(row, "tp"),
        )
        for row in rows[:_PERF_BREAKDOWN_MAX_GROUPS]
    ]

    return AdminRequestPerfBreakdownResponse(
        generated_at=datetime.now(timezone.utc),
        days=days,
        groups=groups,
        truncated=truncated,
    )


async def _get_cached_request_perf_breakdown(
    db_logger,
    *,
    days: int,
    user_id: str | None,
    model_id: str | None,
    request_type: str | None,
    refresh: bool = False,
) -> AdminRequestPerfBreakdownResponse:
    """Load the per-route breakdown, reusing a recent result for these filters."""
    key: _PerfBreakdownKey = (
        id(db_logger.pool),
        days,
        user_id or None,
        model_id or None,
        request_type or None,
    )
    if not refresh:
        cached = _perf_breakdown_cached(key)
        if cached is not None:
            return cached

    async with _perf_breakdown_lock():
        # Re-check inside the lock: a request that queued behind an identical
        # miss takes that result instead of running the same scan again.
        if not refresh:
            cached = _perf_breakdown_cached(key)
            if cached is not None:
                return cached

        response = await _load_request_perf_breakdown(
            db_logger,
            days=days,
            user_id=user_id,
            model_id=model_id,
            request_type=request_type,
        )
        _perf_breakdown_store(key, response)
        return response


@router.get(
    "/recent-requests/performance",
    response_model=AdminRequestPerfBreakdownResponse,
)
async def admin_recent_requests_performance(
    days: int = 7,
    user_id: str | None = None,
    model_id: str | None = None,
    request_type: str | None = None,
    refresh: bool = False,
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> AdminRequestPerfBreakdownResponse:
    """Return TTFT / decode-throughput percentiles per served (model, endpoint).

    Backs the per-route summary above the Recent Requests table, so endpoints
    serving the same model can be compared against each other. See
    :func:`_load_request_perf_breakdown` for which rows are summarized.

    Query Parameters:
    - days: Lookback window in days (default: 7, clamped to [1, 90])
    - user_id: Filter by user ID, name, or email (substring match)
    - model_id: Filter by requested model ID (substring match)
    - request_type: ``"embedding"`` / ``"chat"``, as on ``/recent-requests``
    - refresh: Bypass the short-lived per-filter cache (the panel's Refresh)

    ``errors_only`` and ``status_code`` are deliberately not accepted — this
    view is always scoped to successful requests.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    return await _get_cached_request_perf_breakdown(
        db_logger,
        days=max(1, min(days, 90)),
        user_id=user_id,
        model_id=model_id,
        request_type=request_type,
        refresh=refresh,
    )


@router.get(
    "/recent-requests/{request_id}/content",
    response_model=AdminRecentRequestContentResponse,
)
async def admin_get_recent_request_content(
    request_id: str,
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> AdminRecentRequestContentResponse:
    """Return prompt + response for a single api_logs row.

    Used to lazy-load the expanded view in the admin Recent Requests panel so
    the list endpoint doesn't have to ship those potentially large columns
    for rows the admin never expands.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    async with db_logger.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT prompt, response FROM api_logs WHERE request_id = $1 LIMIT 1",
            request_id,
        )

    if row is None:
        raise HTTPException(404, "Request not found")

    return AdminRecentRequestContentResponse(
        prompt=row["prompt"],
        response=row["response"],
        reasoning_content=extract_reasoning_content(row["response"]),
    )


@router.post("/recent-requests/clear-errors", response_model=AdminClearErrorRequestsResponse)
async def admin_clear_error_requests(
    request: Request,
    hours: int = 1,
    _admin_id: str = Depends(verify_admin_access),
    log_store=Depends(get_log_store),
    op_store=Depends(get_operational_store),
) -> AdminClearErrorRequestsResponse:
    """Hard-delete error requests logged within the last *hours* hours.

    Clears exactly the rows surfaced by the Recent Requests "errors only"
    filter (``error`` set, or a missing / non-2xx-3xx status code). Defaults
    to the past hour. The action is recorded in the admin audit log.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if log_store is None:
        raise HTTPException(500, "Database not configured")

    hours = max(1, min(hours, 24))
    deleted_count = await log_store.delete_recent_error_requests(hours=hours)

    if op_store is not None:
        await log_admin_action(
            op_store,
            get_client_ip(request),
            "clear_error_requests",
            None,
            {"hours": hours, "deleted_count": deleted_count},
        )

    return AdminClearErrorRequestsResponse(
        deleted_count=deleted_count,
        hours=hours,
        message=f"Cleared {deleted_count} error request(s) from the past {hours}h.",
    )


def extract_reasoning_content(response_str: str | None) -> str | None:
    """Extract reasoning_content from a stored OpenAI-style response JSON.

    Returns the concatenated reasoning_content / reasoning string from
    response.choices[*].message.reasoning_content (or .reasoning),
    or response.messages[*].reasoning_content, joined by blank lines.
    Returns None if nothing found or response is not parseable.
    """
    if not response_str:
        return None
    try:
        data = json.loads(response_str)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    pieces: list[str] = []

    def _add_from_message(msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        value = msg.get("reasoning_content")
        if not (isinstance(value, str) and value.strip()):
            value = msg.get("reasoning")
        if isinstance(value, str) and value.strip():
            pieces.append(value.strip())

    choices = data.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict):
                _add_from_message(choice.get("message"))

    messages = data.get("messages")
    if isinstance(messages, list):
        for message in messages:
            _add_from_message(message)

    content = data.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "thinking":
                value = item.get("thinking")
                if isinstance(value, str) and value.strip():
                    pieces.append(value.strip())

    if not pieces:
        return None
    return "\n\n".join(pieces)
