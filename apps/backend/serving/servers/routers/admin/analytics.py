"""Admin analytics endpoint — aggregated request stats for the admin dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BeforeValidator

from serving.analytics.geo_demand import floor_hour, geo_demand_cache
from serving.schemas_admin import (
    AdminAnalyticsResponse,
    AnalyticsBreakdownEntry,
    AnalyticsModelUserEntry,
    AnalyticsModelUsers,
    AnalyticsUserEntry,
    SparklineBucket,
)
from serving.servers.deps import (
    get_db_logger,
    verify_admin_access,
)

router = APIRouter(prefix="/admin")

GeoDays = Annotated[Literal[7, 14, 30, 90], BeforeValidator(int)]


# Period → (lookback_minutes, bucket_minutes)
_ANALYTICS_PERIODS: dict[str, tuple[int, int]] = {
    "hour": (60, 5),
    "day": (1440, 60),
    "week": (10080, 1440),
    "month": (43200, 1440),
}

# Bounds for the per-model top-users breakdown: at most this many models
# (ranked by user-attributed request volume), each with its busiest users.
_TOP_USERS_MODEL_LIMIT = 20
_TOP_USERS_PER_MODEL = 10


@router.get("/analytics/geo")
async def admin_get_geo_analytics(
    days: GeoDays = 14,
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> dict:
    """Return hourly aggregate network-origin demand for the admin globe."""
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    # Cache only complete hours; including the partial current hour would freeze
    # an early snapshot until the one-hour cache entry expires.
    end = floor_hour(datetime.now(timezone.utc))
    start = end - timedelta(days=days)

    return await geo_demand_cache.get(db_logger.pool, start, end)


@router.get("/analytics", response_model=AdminAnalyticsResponse)
async def admin_get_analytics(
    period: Literal["hour", "day", "week", "month"] = "day",
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> AdminAnalyticsResponse:
    """Return analytics summary for the admin analytics tab."""
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    lookback_minutes, bucket_minutes = _ANALYTICS_PERIODS[period]

    # Acquire ONE connection and run all queries sequentially. Acquiring 5
    # connections via asyncio.gather can starve the pool when two admin
    # analytics requests arrive concurrently (pool max_size is small).
    async with db_logger.pool.acquire() as conn:
        # active_users and the mean conversation depth share the same
        # time-window scan over api_logs, so they run as a single query.
        # COUNT(DISTINCT user_id) already ignores NULL user_ids, and AVG()
        # ignores the NULL num_turns / num_user_turns of non-chat requests
        # (embeddings, raw completions), so the averages cover chat requests
        # only and are NULL when the period has none.
        summary_row = await conn.fetchrow(
            """
            SELECT COUNT(DISTINCT user_id) AS active_users,
                   AVG(num_turns) AS avg_turns,
                   AVG(num_user_turns) AS avg_user_turns
            FROM api_logs
            WHERE timestamp >= NOW() - ($1 * interval '1 minute')
            """,
            lookback_minutes,
        )
        active_users = int(summary_row["active_users"] or 0)
        avg_turns = (
            float(summary_row["avg_turns"]) if summary_row["avg_turns"] is not None else None
        )
        avg_user_turns = (
            float(summary_row["avg_user_turns"])
            if summary_row["avg_user_turns"] is not None
            else None
        )

        top_users_rows = await conn.fetch(
            """
            WITH totals AS (
                SELECT COUNT(*) AS grand_total
                FROM api_logs
                WHERE timestamp >= NOW() - ($1 * interval '1 minute')
                  AND user_id IS NOT NULL
            ),
            ranked AS (
                SELECT
                    l.user_id,
                    COALESCE(u.email, l.user_id) AS email,
                    COUNT(*) AS req_count
                FROM api_logs l
                LEFT JOIN users u ON u.id = l.user_id
                WHERE l.timestamp >= NOW() - ($1 * interval '1 minute')
                  AND l.user_id IS NOT NULL
                GROUP BY l.user_id, u.email
                ORDER BY req_count DESC
                LIMIT 10
            )
            SELECT
                r.user_id,
                r.email,
                r.req_count,
                CASE WHEN t.grand_total > 0
                     THEN r.req_count::float / t.grand_total
                     ELSE 0 END AS fraction
            FROM ranked r, totals t
            """,
            lookback_minutes,
        )

        by_model_rows = await conn.fetch(
            """
            WITH totals AS (
                SELECT COUNT(*) AS grand_total
                FROM api_logs
                WHERE timestamp >= NOW() - ($1 * interval '1 minute')
            ),
            ranked AS (
                SELECT model_id AS name, COUNT(*) AS req_count
                FROM api_logs
                WHERE timestamp >= NOW() - ($1 * interval '1 minute')
                GROUP BY model_id
                ORDER BY req_count DESC
                LIMIT 5
            ),
            top_total AS (
                SELECT COALESCE(SUM(req_count), 0) AS top_req_count FROM ranked
            )
            SELECT r.name, r.req_count,
                CASE WHEN t.grand_total > 0 THEN r.req_count::float / t.grand_total ELSE 0 END AS fraction
            FROM ranked r, totals t
            UNION ALL
            SELECT 'others',
                GREATEST(t.grand_total - tt.top_req_count, 0),
                CASE WHEN t.grand_total > 0
                     THEN GREATEST(t.grand_total - tt.top_req_count, 0)::float / t.grand_total
                     ELSE 0 END
            FROM totals t, top_total tt
            WHERE t.grand_total > tt.top_req_count
            ORDER BY req_count DESC
            """,
            lookback_minutes,
        )

        by_provider_rows = await conn.fetch(
            """
            WITH totals AS (
                SELECT COUNT(*) AS grand_total
                FROM api_logs
                WHERE timestamp >= NOW() - ($1 * interval '1 minute')
            ),
            ranked AS (
                SELECT provider AS name, COUNT(*) AS req_count
                FROM api_logs
                WHERE timestamp >= NOW() - ($1 * interval '1 minute')
                GROUP BY provider
                ORDER BY req_count DESC
                LIMIT 4
            ),
            top_total AS (
                SELECT COALESCE(SUM(req_count), 0) AS top_req_count FROM ranked
            )
            SELECT r.name, r.req_count,
                CASE WHEN t.grand_total > 0 THEN r.req_count::float / t.grand_total ELSE 0 END AS fraction
            FROM ranked r, totals t
            UNION ALL
            SELECT 'others',
                GREATEST(t.grand_total - tt.top_req_count, 0),
                CASE WHEN t.grand_total > 0
                     THEN GREATEST(t.grand_total - tt.top_req_count, 0)::float / t.grand_total
                     ELSE 0 END
            FROM totals t, top_total tt
            WHERE t.grand_total > tt.top_req_count
            ORDER BY req_count DESC
            """,
            lookback_minutes,
        )

        # Per-model top users: for each of the busiest models, the users driving
        # the most requests, with their request and token counts. Single scan of
        # the window (per_user), then aggregated in SQL. Scoped to signed-in
        # users (user_id IS NOT NULL), mirroring the top_users query above.
        # Tokens = prompt + completion (total_tokens is only populated when the
        # provider returns it, so SUM(total_tokens) would undercount).
        by_model_top_users_rows = await conn.fetch(
            """
            WITH per_user AS (
                SELECT
                    l.model_id,
                    l.user_id,
                    COALESCE(u.email, l.user_id) AS email,
                    COUNT(*) AS req_count,
                    COALESCE(
                        SUM(COALESCE(l.prompt_tokens, 0) + COALESCE(l.completion_tokens, 0)),
                        0
                    ) AS token_count
                FROM api_logs l
                LEFT JOIN users u ON u.id = l.user_id
                WHERE l.timestamp >= NOW() - ($1 * interval '1 minute')
                  AND l.user_id IS NOT NULL
                GROUP BY l.model_id, l.user_id, u.email
            ),
            model_totals AS (
                -- SUM(bigint) returns numeric (asyncpg Decimal); cast back to int.
                SELECT
                    model_id,
                    SUM(req_count)::bigint AS model_req_count,
                    SUM(token_count)::bigint AS model_token_count
                FROM per_user
                GROUP BY model_id
            ),
            top_models AS (
                SELECT model_id
                FROM model_totals
                ORDER BY model_req_count DESC
                LIMIT $2
            ),
            ranked AS (
                SELECT
                    p.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY p.model_id
                        ORDER BY p.req_count DESC, p.token_count DESC
                    ) AS rn
                FROM per_user p
                JOIN top_models tm ON tm.model_id = p.model_id
            )
            SELECT
                r.model_id,
                r.user_id,
                r.email,
                r.req_count,
                r.token_count,
                mt.model_req_count,
                mt.model_token_count
            FROM ranked r
            JOIN model_totals mt ON mt.model_id = r.model_id
            WHERE r.rn <= $3
            ORDER BY mt.model_req_count DESC, r.model_id, r.req_count DESC
            """,
            lookback_minutes,
            _TOP_USERS_MODEL_LIMIT,
            _TOP_USERS_PER_MODEL,
        )

        # Sparkline: align with the date_trunc + generate_series pattern used by
        # /admin/request-metrics so admin chart bucket boundaries are consistent.
        sparkline_rows = await conn.fetch(
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
                    COUNT(*) AS request_count
                FROM api_logs, bounds
                WHERE timestamp >= bounds.start_time
                  AND timestamp <= bounds.end_time
                GROUP BY 1
            )
            SELECT
                series.bucket_start,
                COALESCE(bucketed_logs.request_count, 0) AS request_count
            FROM series
            LEFT JOIN bucketed_logs
              ON bucketed_logs.bucket_start = series.bucket_start
            ORDER BY series.bucket_start ASC
            """,
            lookback_minutes,
            bucket_minutes,
        )

    # Group the flat per-(model, user) rows into one entry per model. Rows arrive
    # ordered by model_req_count DESC, so dict insertion order preserves the
    # model ranking (model_id is TEXT NOT NULL, so no null-key fallback needed).
    by_model_top_users_map: dict[str, AnalyticsModelUsers] = {}
    for row in by_model_top_users_rows:
        model = str(row["model_id"])
        entry = by_model_top_users_map.get(model)
        if entry is None:
            entry = AnalyticsModelUsers(
                model=model,
                requests=int(row["model_req_count"]),
                tokens=int(row["model_token_count"]),
                users=[],
            )
            by_model_top_users_map[model] = entry
        entry.users.append(
            AnalyticsModelUserEntry(
                email=str(row["email"]),
                user_id=str(row["user_id"]),
                requests=int(row["req_count"]),
                tokens=int(row["token_count"]),
            )
        )

    return AdminAnalyticsResponse(
        period=period,
        active_users=active_users,
        avg_turns=avg_turns,
        avg_user_turns=avg_user_turns,
        sparkline=[
            SparklineBucket(
                start_time=row["bucket_start"],
                request_count=int(row["request_count"] or 0),
            )
            for row in sparkline_rows
        ],
        top_users=[
            AnalyticsUserEntry(
                email=str(row["email"]),
                user_id=str(row["user_id"]),
                requests=int(row["req_count"]),
                fraction=float(row["fraction"]),
            )
            for row in top_users_rows
        ],
        by_model=[
            AnalyticsBreakdownEntry(
                name=str(row["name"]) if row["name"] else "unknown",
                requests=int(row["req_count"]),
                fraction=float(row["fraction"]),
            )
            for row in by_model_rows
        ],
        by_provider=[
            AnalyticsBreakdownEntry(
                name=str(row["name"]) if row["name"] else "unknown",
                requests=int(row["req_count"]),
                fraction=float(row["fraction"]),
            )
            for row in by_provider_rows
        ],
        by_model_top_users=list(by_model_top_users_map.values()),
        generated_at=datetime.now(timezone.utc),
    )
