"""Admin provider endpoints — quotas, hourly stats, and token usage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from serving.admin.provider_quotas import gather_all
from serving.schemas_admin import (
    AdminProviderQuotasResponse,
    ProviderModelPair,
    ProviderStatsResponse,
    ProviderStatsRow,
    ProviderTokenUsageResponse,
    ProviderTokenUsageRow,
    ProviderTokenUsageTotals,
    ProviderTokenUsageWindow,
)
from serving.servers.deps import get_db_logger, verify_admin_access
from serving.servers.routers.admin._common import _require_aware_utc, _truncate_hour

router = APIRouter(prefix="/admin")

_PROVIDER_STATS_MAX_DAYS = 90
_PROVIDER_STATS_DEFAULT_DAYS = 7

_TOKEN_USAGE_RANGES: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

# Matches the CronTrigger(minute=5) of the rollup_provider_stats job in
# serving/admin/provider_stats_rollup.py — the most recent hour bucket
# is not guaranteed to exist until this many minutes past the hour.
_ROLLUP_MINUTE_OFFSET = 5


@router.get("/provider-quotas", response_model=AdminProviderQuotasResponse)
async def admin_provider_quotas(
    _admin_id: str = Depends(verify_admin_access),
) -> AdminProviderQuotasResponse:
    """Return current quota status for each upstream LLM provider."""
    providers = await gather_all()
    return AdminProviderQuotasResponse(
        generated_at=datetime.now(timezone.utc),
        providers=providers,
    )


@router.get("/api/provider-stats", response_model=ProviderStatsResponse)
async def admin_provider_stats(
    request: Request,
    provider: str,
    model_id: str,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> ProviderStatsResponse:
    """Return hourly performance stats for a provider, optionally filtered by model.

    Query Parameters:
        provider: Required upstream provider key (e.g. ``openrouter``).
        model_id: Model identifier, or ``__all__`` to return all models.
        from: ISO8601 lower bound (inclusive). Defaults to ``to - 7 days``.
        to:   ISO8601 upper bound (exclusive). Defaults to current hour.

    The window is hour-truncated and capped at 90 days. The response also
    includes the distinct providers and models seen in the window so the UI
    can populate dropdowns from a single round-trip.
    """
    del request  # accepted to match other admin handlers; pool comes from Depends
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=503, detail="database unavailable")

    now = datetime.now(timezone.utc)
    if to is not None:
        to = _require_aware_utc(to, "to")
    if from_ is not None:
        from_ = _require_aware_utc(from_, "from")

    end = _truncate_hour(to) if to else _truncate_hour(now)
    start = _truncate_hour(from_) if from_ else end - timedelta(days=_PROVIDER_STATS_DEFAULT_DAYS)

    if end <= start:
        raise HTTPException(status_code=400, detail="`to` must be after `from`")
    if (end - start) > timedelta(days=_PROVIDER_STATS_MAX_DAYS):
        raise HTTPException(
            status_code=400,
            detail=f"range must be <= {_PROVIDER_STATS_MAX_DAYS} days",
        )

    fetch_all_models = model_id == "__all__"

    async with db_logger.pool.acquire() as conn:
        if fetch_all_models:
            rows = await conn.fetch(
                """
                SELECT hour_bucket, provider, model_id,
                       request_count, error_count, stream_count,
                       ttft_p50_ms, ttft_p95_ms, ttft_p99_ms,
                       latency_p50_ms, latency_p95_ms, latency_p99_ms,
                       throughput_avg_tps, throughput_p50_tps, throughput_p95_tps,
                       prompt_tokens_avg, completion_tokens_avg, total_completion_tokens,
                       total_prompt_tokens, total_reasoning_tokens
                  FROM provider_hourly_stats
                 WHERE provider = $1
                   AND hour_bucket >= $2 AND hour_bucket < $3
                 ORDER BY model_id, hour_bucket ASC
                """,
                provider,
                start,
                end,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT hour_bucket, provider, model_id,
                       request_count, error_count, stream_count,
                       ttft_p50_ms, ttft_p95_ms, ttft_p99_ms,
                       latency_p50_ms, latency_p95_ms, latency_p99_ms,
                       throughput_avg_tps, throughput_p50_tps, throughput_p95_tps,
                       prompt_tokens_avg, completion_tokens_avg, total_completion_tokens,
                       total_prompt_tokens, total_reasoning_tokens
                  FROM provider_hourly_stats
                 WHERE provider = $1 AND model_id = $2
                   AND hour_bucket >= $3 AND hour_bucket < $4
                 ORDER BY hour_bucket ASC
                """,
                provider,
                model_id,
                start,
                end,
            )
        providers = await conn.fetch(
            """
            SELECT DISTINCT provider FROM provider_hourly_stats
             WHERE hour_bucket >= $1 AND hour_bucket < $2
             ORDER BY provider
            """,
            start,
            end,
        )
        models = await conn.fetch(
            """
            SELECT DISTINCT model_id FROM provider_hourly_stats
             WHERE hour_bucket >= $1 AND hour_bucket < $2
             ORDER BY model_id
            """,
            start,
            end,
        )
        pairs = await conn.fetch(
            """
            SELECT DISTINCT provider, model_id FROM provider_hourly_stats
             WHERE hour_bucket >= $1 AND hour_bucket < $2
             ORDER BY provider, model_id
            """,
            start,
            end,
        )

    return ProviderStatsResponse(
        rows=[ProviderStatsRow(**dict(r)) for r in rows],
        providers=[r["provider"] for r in providers],
        models=[r["model_id"] for r in models],
        pairs=[ProviderModelPair(provider=r["provider"], model_id=r["model_id"]) for r in pairs],
    )


# ============================================================
# Token Usage tab — per (provider, model_id) totals over a fixed-window
# selector (1h | 24h | 7d | 30d). Reads pre-aggregated rows from
# provider_hourly_stats; no scan of api_logs.
# ============================================================


@router.get("/api/provider-token-usage", response_model=ProviderTokenUsageResponse)
async def admin_provider_token_usage(
    request: Request,
    range: Literal["1h", "24h", "7d", "30d"] = "24h",
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> ProviderTokenUsageResponse:
    """Per-(provider, model_id) token totals + cost over a fixed window.

    Query parameters:
        range: one of "1h", "24h", "7d", "30d". Defaults to "24h".

    The window is hour-truncated; `from = floor(now, hour) - <range>`,
    `to = floor(now, hour)`. Rows are sorted by total token sum
    (input + output + cached + reasoning) descending. Totals are
    summed in Python from the same rows to avoid a second DB hit.
    """
    del request
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=503, detail="database unavailable")

    delta = _TOKEN_USAGE_RANGES[range]
    # Rollup runs at minute :05, so during [HH:00, HH:05) the bucket for
    # hour HH has not been written yet. Subtract one hour from `end` in
    # that window so we don't undercount and so `refreshed_at` reflects
    # the most recent bucket guaranteed to exist.
    now = datetime.now(timezone.utc)
    end = _truncate_hour(now)
    if now.minute < _ROLLUP_MINUTE_OFFSET:
        end = end - timedelta(hours=1)
    start = end - delta

    async with db_logger.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                provider,
                model_id,
                COALESCE(SUM(total_prompt_tokens), 0)::BIGINT      AS input_tokens,
                COALESCE(SUM(total_completion_tokens), 0)::BIGINT  AS output_tokens,
                COALESCE(SUM(total_cache_read_tokens), 0)::BIGINT  AS cached_tokens,
                COALESCE(SUM(total_reasoning_tokens), 0)::BIGINT   AS reasoning_tokens,
                COALESCE(SUM(total_cost_usd), 0)                   AS cost_usd,
                COALESCE(SUM(request_count), 0)::BIGINT            AS request_count
            FROM provider_hourly_stats
            WHERE hour_bucket >= $1 AND hour_bucket < $2
            GROUP BY provider, model_id
            ORDER BY (
                  COALESCE(SUM(total_prompt_tokens), 0)
                + COALESCE(SUM(total_completion_tokens), 0)
                + COALESCE(SUM(total_cache_read_tokens), 0)
                + COALESCE(SUM(total_reasoning_tokens), 0)
            ) DESC
            """,
            start,
            end,
        )

    out_rows = [ProviderTokenUsageRow(**dict(r)) for r in rows]
    totals = ProviderTokenUsageTotals(
        input_tokens=sum(r.input_tokens for r in out_rows),
        output_tokens=sum(r.output_tokens for r in out_rows),
        cached_tokens=sum(r.cached_tokens for r in out_rows),
        reasoning_tokens=sum(r.reasoning_tokens for r in out_rows),
        cost_usd=sum(r.cost_usd for r in out_rows),
        request_count=sum(r.request_count for r in out_rows),
    )

    return ProviderTokenUsageResponse(
        range=range,
        window=ProviderTokenUsageWindow.model_validate({"from": start, "to": end}),
        refreshed_at=end,
        rows=out_rows,
        totals=totals,
    )
