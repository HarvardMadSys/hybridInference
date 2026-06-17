"""Admin per-request and per-route metrics endpoints."""

from __future__ import annotations

import json
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


def _distribution_from_row(
    row: Any,
    prefix: str,
    edges: tuple[float, ...],
    bucket_counts: dict[int, int],
) -> AdminMetricDistribution:
    """Build a distribution from a stats row + histogram counts dict."""
    return AdminMetricDistribution(
        count=int(row[f"{prefix}_count"] or 0),
        mean=_round_or_none(row[f"{prefix}_mean"]),
        min=_round_or_none(row[f"{prefix}_min"]),
        max=_round_or_none(row[f"{prefix}_max"]),
        p50=_round_or_none(row[f"{prefix}_p50"]),
        p90=_round_or_none(row[f"{prefix}_p90"]),
        p95=_round_or_none(row[f"{prefix}_p95"]),
        p99=_round_or_none(row[f"{prefix}_p99"]),
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


def _escape_ilike_substring_term(term: str) -> str:
    """Escape LIKE wildcards so ``ILIKE`` performs literal substring matching."""
    return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


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


@router.get("/performance-metrics", response_model=AdminPerformanceMetricsResponse)
async def admin_get_performance_metrics(
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> AdminPerformanceMetricsResponse:
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
                    percentile_cont(0.5) WITHIN GROUP (
                        ORDER BY prompt_tokens
                    ) FILTER (
                        WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0
                    ) AS pt_p50,
                    percentile_cont(0.9) WITHIN GROUP (
                        ORDER BY prompt_tokens
                    ) FILTER (
                        WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0
                    ) AS pt_p90,
                    percentile_cont(0.95) WITHIN GROUP (
                        ORDER BY prompt_tokens
                    ) FILTER (
                        WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0
                    ) AS pt_p95,
                    percentile_cont(0.99) WITHIN GROUP (
                        ORDER BY prompt_tokens
                    ) FILTER (
                        WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0
                    ) AS pt_p99,

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
                    percentile_cont(0.5) WITHIN GROUP (
                        ORDER BY completion_tokens
                    ) FILTER (
                        WHERE completion_tokens IS NOT NULL AND completion_tokens > 0
                    ) AS ct_p50,
                    percentile_cont(0.9) WITHIN GROUP (
                        ORDER BY completion_tokens
                    ) FILTER (
                        WHERE completion_tokens IS NOT NULL AND completion_tokens > 0
                    ) AS ct_p90,
                    percentile_cont(0.95) WITHIN GROUP (
                        ORDER BY completion_tokens
                    ) FILTER (
                        WHERE completion_tokens IS NOT NULL AND completion_tokens > 0
                    ) AS ct_p95,
                    percentile_cont(0.99) WITHIN GROUP (
                        ORDER BY completion_tokens
                    ) FILTER (
                        WHERE completion_tokens IS NOT NULL AND completion_tokens > 0
                    ) AS ct_p99,

                    COUNT(*) FILTER (WHERE ttft_ms IS NOT NULL) AS tt_count,
                    AVG(ttft_ms) FILTER (WHERE ttft_ms IS NOT NULL) AS tt_mean,
                    MIN(ttft_ms) FILTER (WHERE ttft_ms IS NOT NULL) AS tt_min,
                    MAX(ttft_ms) FILTER (WHERE ttft_ms IS NOT NULL) AS tt_max,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY ttft_ms)
                        FILTER (WHERE ttft_ms IS NOT NULL) AS tt_p50,
                    percentile_cont(0.9) WITHIN GROUP (ORDER BY ttft_ms)
                        FILTER (WHERE ttft_ms IS NOT NULL) AS tt_p90,
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY ttft_ms)
                        FILTER (WHERE ttft_ms IS NOT NULL) AS tt_p95,
                    percentile_cont(0.99) WITHIN GROUP (ORDER BY ttft_ms)
                        FILTER (WHERE ttft_ms IS NOT NULL) AS tt_p99,

                    COUNT(*) FILTER (WHERE throughput_tps IS NOT NULL) AS tp_count,
                    AVG(throughput_tps) FILTER (WHERE throughput_tps IS NOT NULL) AS tp_mean,
                    MIN(throughput_tps) FILTER (WHERE throughput_tps IS NOT NULL) AS tp_min,
                    MAX(throughput_tps) FILTER (WHERE throughput_tps IS NOT NULL) AS tp_max,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY throughput_tps)
                        FILTER (WHERE throughput_tps IS NOT NULL) AS tp_p50,
                    percentile_cont(0.9) WITHIN GROUP (ORDER BY throughput_tps)
                        FILTER (WHERE throughput_tps IS NOT NULL) AS tp_p90,
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY throughput_tps)
                        FILTER (WHERE throughput_tps IS NOT NULL) AS tp_p95,
                    percentile_cont(0.99) WITHIN GROUP (ORDER BY throughput_tps)
                        FILTER (WHERE throughput_tps IS NOT NULL) AS tp_p99
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
    admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> AdminRecentRequestsResponse:
    """List recent API requests across all users.

    Query Parameters:
    - limit: Max results (default: 50, max: 200)
    - offset: Pagination offset
    - days: Lookback window in days (default: 7, clamped to [1, 90])
    - user_id: Filter by user ID
    - model_id: Filter by model ID
    - status_code: Filter by HTTP status code
    - errors_only: If true, only show requests with errors

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    days = max(1, min(days, 90))

    # Build WHERE clause — always bound by lookback window so neither the
    # COUNT nor the SELECT scans the full retention range.
    where_clauses: list[str] = []
    params: list[Any] = [days]
    where_clauses.append(f"l.timestamp >= NOW() - make_interval(days => ${len(params)}::int)")

    if user_id:
        where_clauses.append(f"l.user_id = ${len(params) + 1}")
        params.append(user_id)

    if model_id:
        params.append(_escape_ilike_substring_term(model_id))
        where_clauses.append(f"l.model_id ILIKE '%' || ${len(params)} || '%' ESCAPE '\\'")

    if status_code is not None:
        where_clauses.append(f"l.status_code = ${len(params) + 1}")
        params.append(status_code)

    if errors_only:
        where_clauses.append(
            "(l.error IS NOT NULL OR l.status_code IS NULL "
            "OR l.status_code < 200 OR l.status_code >= 400)"
        )

    where_sql = "WHERE " + " AND ".join(where_clauses)

    async with db_logger.pool.acquire() as conn:
        # Get total count
        count_row = await conn.fetchrow(
            f"SELECT COUNT(*) as total FROM api_logs l {where_sql}",
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
                l.metadata->>'session_id' AS session_id,
                l.metadata->>'surface' AS request_surface,
                l.metadata->>'request_type' AS request_type,
                l.metadata->'routewise' AS routewise,
                CASE
                    WHEN jsonb_typeof(l.request_payload->'messages') = 'array'
                    THEN jsonb_array_length(l.request_payload->'messages')
                END AS num_turns,
                CASE
                    WHEN jsonb_typeof(l.request_payload->'messages') = 'array'
                    THEN (
                        SELECT count(*)::int
                        FROM jsonb_array_elements(l.request_payload->'messages') AS m
                        WHERE m->>'role' = 'user'
                    )
                END AS num_user_turns,
                CASE
                    WHEN jsonb_typeof(l.request_payload->'messages') = 'array'
                    THEN (
                        SELECT coalesce(sum(jsonb_array_length(m->'tool_calls')), 0)::int
                        FROM jsonb_array_elements(l.request_payload->'messages') AS m
                        WHERE jsonb_typeof(m->'tool_calls') = 'array'
                    )
                END AS num_tool_calls
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
