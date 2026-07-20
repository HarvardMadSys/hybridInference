"""Admin provider endpoints — quotas, hourly stats, and token usage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from routing.endpoints import endpoint_id_for_adapter
from serving.admin.provider_quotas import gather_all
from serving.schemas_admin import (
    AdminProviderQuotasResponse,
    ListRoutableProvidersResponse,
    ProviderErrorTypeRow,
    ProviderModelPair,
    ProviderObservabilityBucket,
    ProviderObservabilityResponse,
    ProviderObservabilityTotals,
    ProviderObservabilityWindow,
    ProviderStatsResponse,
    ProviderStatsRow,
    ProviderTokenUsageResponse,
    ProviderTokenUsageRow,
    ProviderTokenUsageTotals,
    ProviderTokenUsageWindow,
    RoutableProvider,
    SetProviderDisabledRequest,
    SetProviderDisabledResponse,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import (
    get_db_logger,
    get_operational_store,
    get_services,
    verify_admin_access,
)
from serving.servers.routers.admin._common import _require_aware_utc, _truncate_hour
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/admin")

_PROVIDER_STATS_MAX_DAYS = 90
_PROVIDER_STATS_DEFAULT_DAYS = 7
_SYNTHETIC_PERFORMANCE_PROVIDERS = frozenset({"", "router"})
_ERROR_CONDITION_SQL = (
    "(error IS NOT NULL OR status_code IS NULL OR status_code < 200 OR status_code >= 400)"
)
_OBSERVABILITY_LOG_SCOPE_SQL = "(metadata->>'request_type') IS DISTINCT FROM 'embedding'"
_ERROR_TYPE_SQL = """
CASE
    WHEN error = 'quota_exceeded' THEN 'quota_exceeded'
    WHEN error = 'concurrency_limit_exceeded' THEN 'concurrency_limit'
    WHEN error = 'model_not_found' OR error ILIKE 'Model % not found%' THEN 'model_not_found'
    WHEN status_code = 429 OR error ILIKE '%429%' OR error ILIKE '%rate%limit%'
         OR error ILIKE '%TooManyRequests%' THEN 'rate_limited'
    WHEN status_code = 504 OR error ILIKE '%timeout%' OR error ILIKE '%timed out%' THEN 'timeout'
    WHEN status_code IN (401, 403) OR error ILIKE 'auth_%' THEN 'auth'
    WHEN status_code IN (400, 422) OR error ILIKE '%validation%' THEN 'validation'
    WHEN status_code = 404 THEN 'not_found'
    WHEN status_code >= 500 THEN 'server_error'
    ELSE 'unknown'
END
"""

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


def _is_reportable_performance_provider(provider: str) -> bool:
    """Return whether a provider label represents a real upstream provider."""
    return provider not in _SYNTHETIC_PERFORMANCE_PROVIDERS


async def _disabled_provider_set(op_store) -> set[str]:
    """Return the set of admin-disabled provider labels (empty on failure)."""
    if op_store is None:
        return set()
    try:
        rows = await op_store.list_disabled_providers()
    except Exception:
        return set()
    return {str(row["provider"]) for row in rows}


def _enumerate_routable_providers(
    router_obj,
) -> dict[str, tuple[set[str], set[str]]]:
    """Map each provider label to its (canonical model ids, endpoint ids).

    Iterates the live routing table. Alias model ids share a RouteConfig with
    their canonical model, so counting by ``canonical_model_id`` avoids double
    counting aliases.
    """
    by_provider: dict[str, tuple[set[str], set[str]]] = {}
    for model_id, route in getattr(router_obj, "routes", {}).items():
        canonical = getattr(route, "canonical_model_id", None) or model_id
        for adapter, _weight in route.adapters:
            provider = adapter.config.provider
            if not _is_reportable_performance_provider(provider):
                continue
            models, endpoints = by_provider.setdefault(provider, (set(), set()))
            models.add(canonical)
            endpoints.add(endpoint_id_for_adapter(adapter))
    return by_provider


def _observability_bucket_minutes(start: datetime, end: datetime) -> int:
    """Choose a compact bucket size for provider observability charts."""
    delta = end - start
    if delta <= timedelta(hours=2):
        return 5
    if delta <= timedelta(days=2):
        return 60
    if delta <= timedelta(days=8):
        return 360
    return 1440


@router.get("/provider-quotas", response_model=AdminProviderQuotasResponse)
async def admin_provider_quotas(
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    services=Depends(get_services),
) -> AdminProviderQuotasResponse:
    """Return current quota status for each upstream LLM provider."""
    providers = await gather_all(op_store, services)
    disabled = await _disabled_provider_set(op_store)
    for provider in providers:
        provider.disabled = provider.name in disabled
    return AdminProviderQuotasResponse(
        generated_at=datetime.now(timezone.utc),
        providers=providers,
    )


@router.get("/providers/routable", response_model=ListRoutableProvidersResponse)
async def admin_list_routable_providers(
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    services=Depends(get_services),
) -> ListRoutableProvidersResponse:
    """List every provider in the live routing table with its disabled state."""
    disabled = await _disabled_provider_set(op_store)
    by_provider = _enumerate_routable_providers(services.router)
    # Include disabled providers even if they've since been removed from the
    # routing table, so an admin can always find and re-enable them.
    for provider in disabled:
        by_provider.setdefault(provider, (set(), set()))

    providers = [
        RoutableProvider(
            provider=provider,
            model_count=len(models),
            endpoint_count=len(endpoints),
            disabled=provider in disabled,
        )
        for provider, (models, endpoints) in sorted(by_provider.items())
    ]
    return ListRoutableProvidersResponse(providers=providers)


@router.patch(
    "/providers/{provider}/disabled",
    response_model=SetProviderDisabledResponse,
)
async def admin_set_provider_disabled(
    request: Request,
    provider: str,
    payload: SetProviderDisabledRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    services=Depends(get_services),
) -> SetProviderDisabledResponse:
    """Disable or re-enable an upstream provider across all routing."""
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    by_provider = _enumerate_routable_providers(services.router)
    disabled = await _disabled_provider_set(op_store)
    # Guard against typos: only accept a provider that is routable now or is
    # already recorded as disabled (so it can be re-enabled).
    if provider not in by_provider and provider not in disabled:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")

    affected_models = by_provider.get(provider, (set(), set()))[0]

    resolver = getattr(services, "disabled_provider_resolver", None)
    if payload.disabled:
        await op_store.set_provider_disabled(provider, admin_id)
        if resolver is not None:
            resolver.set_disabled(provider)
    else:
        await op_store.clear_provider_disabled(provider)
        if resolver is not None:
            resolver.clear_disabled(provider)

    await log_admin_action(
        op_store,
        get_client_ip(request),
        "providers.disabled.update",
        provider,
        {"provider": provider, "disabled": payload.disabled},
    )

    return SetProviderDisabledResponse(
        provider=provider,
        disabled=payload.disabled,
        affected_model_count=len(affected_models),
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
    includes the distinct providers and models across the full retained table
    (last 30 days), independent of the selected range, so the UI can populate
    its dropdowns from a single round-trip even when the chosen window has no
    rows.
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
        # Dropdown lists span the full retained table (purge caps it at 30
        # days), NOT the selected [start, end) window — otherwise picking a
        # range with no rows would leave the provider dropdown empty. One
        # DISTINCT scan over the (provider, model_id) pairs is enough; the
        # provider and model lists are derived from it in Python.
        pairs = await conn.fetch(
            """
            SELECT DISTINCT provider, model_id FROM provider_hourly_stats
             ORDER BY provider, model_id
            """
        )
        # Providers with rows INSIDE the selected window — used by the UI to
        # pick a sensible default so the tab doesn't open on a provider that
        # has no in-range data.
        window_providers = await conn.fetch(
            """
            SELECT DISTINCT provider FROM provider_hourly_stats
             WHERE hour_bucket >= $1 AND hour_bucket < $2
             ORDER BY provider
            """,
            start,
            end,
        )

    rows = [r for r in rows if _is_reportable_performance_provider(r["provider"])]
    pairs = [r for r in pairs if _is_reportable_performance_provider(r["provider"])]
    window_providers = [
        r for r in window_providers if _is_reportable_performance_provider(r["provider"])
    ]

    providers = sorted({r["provider"] for r in pairs})
    models = sorted({r["model_id"] for r in pairs})

    return ProviderStatsResponse(
        rows=[ProviderStatsRow(**dict(r)) for r in rows],
        providers=providers,
        models=models,
        pairs=[ProviderModelPair(provider=r["provider"], model_id=r["model_id"]) for r in pairs],
        window_providers=[r["provider"] for r in window_providers],
    )


# ============================================================
# Provider Observability — provider-scoped error + cache stats directly
# from api_logs. Kept separate from provider_hourly_stats so classification
# changes don't require a schema migration or backfill.
# ============================================================


@router.get("/api/provider-observability", response_model=ProviderObservabilityResponse)
async def admin_provider_observability(
    request: Request,
    provider: str,
    model_id: str = "__all__",
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> ProviderObservabilityResponse:
    """Return provider-scoped error and prompt-cache stats over a bounded window."""
    del request
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=503, detail="database unavailable")
    if not _is_reportable_performance_provider(provider):
        raise HTTPException(status_code=400, detail="provider must be an upstream provider")

    now = datetime.now(timezone.utc)
    if to is not None:
        to = _require_aware_utc(to, "to")
    if from_ is not None:
        from_ = _require_aware_utc(from_, "from")

    end = to if to else now
    start = from_ if from_ else end - timedelta(days=_PROVIDER_STATS_DEFAULT_DAYS)

    if end <= start:
        raise HTTPException(status_code=400, detail="`to` must be after `from`")
    if (end - start) > timedelta(days=_PROVIDER_STATS_MAX_DAYS):
        raise HTTPException(
            status_code=400,
            detail=f"range must be <= {_PROVIDER_STATS_MAX_DAYS} days",
        )

    bucket_minutes = _observability_bucket_minutes(start, end)

    async with db_logger.pool.acquire() as conn:
        totals_row = await conn.fetchrow(
            f"""
            SELECT
                COUNT(*)::BIGINT AS request_count,
                COUNT(*) FILTER (WHERE {_ERROR_CONDITION_SQL})::BIGINT AS error_count,
                COUNT(*) FILTER (
                    WHERE status_code = 429 OR error ILIKE '%429%'
                       OR error ILIKE '%rate%limit%' OR error ILIKE '%TooManyRequests%'
                )::BIGINT AS rate_limited_count,
                COUNT(*) FILTER (
                    WHERE status_code = 504 OR error ILIKE '%timeout%' OR error ILIKE '%timed out%'
                )::BIGINT AS timeout_count,
                COUNT(*) FILTER (WHERE status_code >= 500)::BIGINT AS server_error_count,
                COUNT(*) FILTER (
                    WHERE status_code >= 200 AND status_code < 400
                      AND COALESCE(prompt_tokens, 0) > 0
                )::BIGINT AS cache_eligible_count,
                COUNT(*) FILTER (
                    WHERE status_code >= 200 AND status_code < 400
                      AND COALESCE(prompt_tokens, 0) > 0
                      AND COALESCE(cache_read_tokens, 0) > 0
                )::BIGINT AS cache_hit_count,
                COALESCE(SUM(prompt_tokens) FILTER (
                    WHERE status_code >= 200 AND status_code < 400
                ), 0)::BIGINT AS input_tokens,
                COALESCE(SUM(cache_read_tokens) FILTER (
                    WHERE status_code >= 200 AND status_code < 400
                ), 0)::BIGINT AS cache_read_tokens,
                COALESCE(SUM(cache_write_tokens) FILTER (
                    WHERE status_code >= 200 AND status_code < 400
                ), 0)::BIGINT AS cache_write_tokens
            FROM api_logs
            WHERE provider = $1
              AND timestamp >= $2 AND timestamp < $3
              AND ($4::text = '__all__' OR model_id = $4::text)
              AND {_OBSERVABILITY_LOG_SCOPE_SQL}
            """,
            provider,
            start,
            end,
            model_id,
        )
        bucket_rows = await conn.fetch(
            f"""
            WITH config AS (
                SELECT ($5::int * 60) AS bucket_seconds
            ),
            bounds AS (
                SELECT
                    to_timestamp(
                        floor(extract(epoch FROM $2::timestamptz) / config.bucket_seconds)
                        * config.bucket_seconds
                    ) AS aligned_start,
                    $3::timestamptz AS end_time
                FROM config
            ),
            series AS (
                SELECT generate_series(
                    (SELECT aligned_start FROM bounds),
                    (SELECT end_time FROM bounds),
                    $5::int * interval '1 minute'
                ) AS bucket_start
            ),
            bucketed_logs AS (
                SELECT
                    to_timestamp(
                        floor(extract(epoch FROM timestamp) / ($5::int * 60))
                        * ($5::int * 60)
                    ) AS bucket_start,
                    COUNT(*)::BIGINT AS request_count,
                    COUNT(*) FILTER (
                        WHERE error IS NOT NULL
                           OR status_code IS NULL
                           OR status_code < 200
                           OR status_code >= 400
                    )::BIGINT AS error_count,
                    COUNT(*) FILTER (
                        WHERE status_code >= 200 AND status_code < 400
                          AND COALESCE(prompt_tokens, 0) > 0
                    )::BIGINT AS cache_eligible_count,
                    COUNT(*) FILTER (
                        WHERE status_code >= 200 AND status_code < 400
                          AND COALESCE(prompt_tokens, 0) > 0
                          AND COALESCE(cache_read_tokens, 0) > 0
                    )::BIGINT AS cache_hit_count,
                    COALESCE(SUM(cache_read_tokens) FILTER (
                        WHERE status_code >= 200 AND status_code < 400
                    ), 0)::BIGINT AS cache_read_tokens,
                    COALESCE(SUM(prompt_tokens) FILTER (
                        WHERE status_code >= 200 AND status_code < 400
                    ), 0)::BIGINT AS input_tokens
                FROM api_logs
                WHERE provider = $1
                  AND timestamp >= $2 AND timestamp < $3
                  AND ($4::text = '__all__' OR model_id = $4::text)
                  AND {_OBSERVABILITY_LOG_SCOPE_SQL}
                GROUP BY 1
            )
            SELECT
                series.bucket_start AS start_time,
                COALESCE(bucketed_logs.request_count, 0) AS request_count,
                COALESCE(bucketed_logs.error_count, 0) AS error_count,
                COALESCE(bucketed_logs.cache_eligible_count, 0) AS cache_eligible_count,
                COALESCE(bucketed_logs.cache_hit_count, 0) AS cache_hit_count,
                COALESCE(bucketed_logs.cache_read_tokens, 0) AS cache_read_tokens,
                COALESCE(bucketed_logs.input_tokens, 0) AS input_tokens
            FROM series
            LEFT JOIN bucketed_logs ON bucketed_logs.bucket_start = series.bucket_start
            WHERE series.bucket_start < $3
            ORDER BY series.bucket_start ASC
            """,
            provider,
            start,
            end,
            model_id,
            bucket_minutes,
        )
        error_type_rows = await conn.fetch(
            f"""
            SELECT error_type, COUNT(*)::BIGINT AS count
            FROM (
                SELECT {_ERROR_TYPE_SQL} AS error_type
                FROM api_logs
                WHERE provider = $1
                  AND timestamp >= $2 AND timestamp < $3
                  AND ($4::text = '__all__' OR model_id = $4::text)
                  AND {_OBSERVABILITY_LOG_SCOPE_SQL}
                  AND {_ERROR_CONDITION_SQL}
            ) typed
            GROUP BY error_type
            ORDER BY count DESC, error_type ASC
            """,
            provider,
            start,
            end,
            model_id,
        )

    totals = ProviderObservabilityTotals(**dict(totals_row or {}))
    total_errors = max(totals.error_count, 1)
    return ProviderObservabilityResponse(
        provider=provider,
        window=ProviderObservabilityWindow.model_validate({"from": start, "to": end}),
        bucket_minutes=bucket_minutes,
        totals=totals,
        buckets=[ProviderObservabilityBucket(**dict(r)) for r in bucket_rows],
        error_types=[
            ProviderErrorTypeRow(
                error_type=r["error_type"],
                count=int(r["count"] or 0),
                fraction=float(int(r["count"] or 0) / total_errors),
            )
            for r in error_type_rows
        ],
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
              AND provider NOT IN ('', 'router')
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

    out_rows: list[ProviderTokenUsageRow] = []
    for row in rows:
        row_dict = dict(row)
        if row_dict.get("provider") in {"", "router"}:
            continue
        out_rows.append(ProviderTokenUsageRow(**row_dict))
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
