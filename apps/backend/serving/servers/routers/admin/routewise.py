"""Dedicated admin Routewise runtime settings endpoints."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request

from routing.routewise.router import RouteWiseRouter
from serving.config.runtime_settings import (
    RUNTIME_SETTINGS_REGISTRY,
    RuntimeSettings,
    get_runtime_settings,
)
from serving.schemas_admin import (
    ListRoutewiseProbeSamplesResponse,
    ListRoutewiseSettingsResponse,
    RoutewiseDecisionBucket,
    RoutewiseDecisionBucketHedge,
    RoutewiseDecisionsResponse,
    RoutewiseHedgeSummary,
    RoutewiseProbeRunResult,
    RoutewiseProbeSampleItem,
    RoutewiseSelectionShareItem,
    RoutewiseSettingItem,
    RunRoutewiseProbeRequest,
    RunRoutewiseProbeResponse,
    UpdateSettingRequest,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import (
    get_db_logger,
    get_operational_store,
    get_services,
    verify_admin_access,
)
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/admin/routewise")

ROUTEWISE_KEYS = (
    "routewise_budget_alpha",
    "routewise_latency_slo_sec",
    "routewise_latency_min_samples",
    "routewise_probe_enabled",
    "routewise_probe_interval_sec",
)

# Maps each decisions range to its (lookback window, time-bucket) size in seconds.
DECISIONS_RANGE_SECONDS: dict[str, tuple[int, int]] = {
    "24h": (86_400, 3_600),
    "7d": (604_800, 21_600),
    "30d": (2_592_000, 86_400),
}

# The endpoint that actually served a request. A hedge backup win is served by
# ``backup_provider`` (its tier in ``backup_provider_type``); every other request
# is served by ``final_endpoint`` (``final_provider_type``). Defined once and
# interpolated into the counts, selection, and bucket queries so the three usages
# cannot drift. These are static SQL (no user input), safe to interpolate.
SERVED_ENDPOINT_SQL = (
    "CASE WHEN (metadata->'routewise'->>'hedge_winner') = 'backup' "
    "THEN COALESCE(metadata->'routewise'->>'backup_provider', "
    "metadata->'routewise'->>'final_endpoint') "
    "ELSE metadata->'routewise'->>'final_endpoint' END"
)
SERVED_PROVIDER_TYPE_SQL = (
    "CASE WHEN (metadata->'routewise'->>'hedge_winner') = 'backup' "
    "THEN COALESCE(metadata->'routewise'->>'backup_provider_type', "
    "metadata->'routewise'->>'final_provider_type') "
    "ELSE metadata->'routewise'->>'final_provider_type' END"
)


def _require_runtime_settings(rt: RuntimeSettings | None) -> RuntimeSettings:
    """Return the singleton or raise 503 if the app hasn't initialized it yet."""
    if rt is None:
        raise HTTPException(status_code=503, detail="Runtime settings not initialized")
    return rt


def _serialize_existing_value(raw: str | None, expected_type: str) -> Any:
    if raw is None:
        return None
    if expected_type == "bool":
        return raw.lower() in ("true", "1", "yes") if isinstance(raw, str) else raw
    try:
        if expected_type == "int":
            return int(raw)
        if expected_type == "float":
            return float(raw)
    except (TypeError, ValueError):
        return raw
    return raw


def _routewise_routers_for_probe(services: Any, model_id: str | None) -> list[RouteWiseRouter]:
    registry = getattr(services, "model_router_registry", None)
    if registry is None:
        return []
    if model_id:
        router_obj = registry.get_router(model_id)
        return [router_obj] if isinstance(router_obj, RouteWiseRouter) else []
    for configured_model_id in registry.configured_model_ids():
        if registry.get_router_name(configured_model_id) == "routewise":
            registry.get_router(configured_model_id)
    seen: set[int] = set()
    routers: list[RouteWiseRouter] = []
    for router_obj in registry.cached_routers():
        if isinstance(router_obj, RouteWiseRouter) and id(router_obj) not in seen:
            routers.append(router_obj)
            seen.add(id(router_obj))
    return routers


async def _refresh_live_routewise_routers(request: Request, rt: RuntimeSettings) -> None:
    """Refresh cached RouteWise router instances from current runtime settings."""
    services = getattr(request.app.state, "services", None)
    registry = getattr(services, "model_router_registry", None)
    if registry is None:
        return

    for key in ROUTEWISE_KEYS:
        rt.invalidate_key(key)

    budget_alpha = await rt.get_float("routewise_budget_alpha")
    latency_slo_sec = await rt.get_float("routewise_latency_slo_sec")
    latency_min_samples = await rt.get_int("routewise_latency_min_samples")
    routewise_probe_enabled = await rt.get_bool("routewise_probe_enabled")
    routewise_probe_interval_sec = await rt.get_float("routewise_probe_interval_sec")

    for model_id in registry.configured_model_ids():
        if registry.get_router_name(model_id) != "routewise":
            continue
        registry.get_router(model_id)

    for router in registry.cached_routers():
        if isinstance(router, RouteWiseRouter):
            router.apply_runtime_overrides(
                budget_alpha=budget_alpha,
                latency_slo_sec=latency_slo_sec,
                latency_min_samples=latency_min_samples,
                routewise_probe_enabled=routewise_probe_enabled,
                routewise_probe_interval_sec=routewise_probe_interval_sec,
            )
            await router.refresh_probe_task()


@router.get("/settings", response_model=ListRoutewiseSettingsResponse)
async def list_routewise_settings_endpoint(
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    rt: RuntimeSettings | None = Depends(get_runtime_settings),
) -> ListRoutewiseSettingsResponse:
    """List the curated Routewise runtime settings."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    rt = _require_runtime_settings(rt)

    items_by_key = {item["key"]: item for item in await rt.list_all()}
    return ListRoutewiseSettingsResponse(
        settings=[
            RoutewiseSettingItem(
                key=key,
                value=items_by_key[key]["value"],
                value_type=items_by_key[key]["value_type"],
                default_value=items_by_key[key]["default_value"],
                description=items_by_key[key]["description"],
                min=items_by_key[key].get("min"),
                max=items_by_key[key].get("max"),
            )
            for key in ROUTEWISE_KEYS
        ]
    )


@router.patch("/settings/{key}", response_model=RoutewiseSettingItem)
async def update_routewise_setting_endpoint(
    request: Request,
    key: str,
    payload: UpdateSettingRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    rt: RuntimeSettings | None = Depends(get_runtime_settings),
) -> RoutewiseSettingItem:
    """Update a single curated Routewise runtime setting by key."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    rt = _require_runtime_settings(rt)

    if key not in ROUTEWISE_KEYS:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")

    entry = RUNTIME_SETTINGS_REGISTRY.get(key)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")

    expected_type = entry["type"]
    value = payload.value
    if expected_type == "bool" and not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"Setting '{key}' expects a boolean value")
    if expected_type == "int" and (not isinstance(value, int) or isinstance(value, bool)):
        raise HTTPException(status_code=400, detail=f"Setting '{key}' expects an integer value")
    if expected_type == "float" and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        raise HTTPException(status_code=400, detail=f"Setting '{key}' expects a numeric value")
    if expected_type == "str" and not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"Setting '{key}' expects a string value")

    if expected_type in ("int", "float"):
        lo = entry.get("min")
        hi = entry.get("max")
        if lo is not None and value < lo:
            raise HTTPException(
                status_code=400,
                detail=f"Setting '{key}' value {value} is below min ({lo})",
            )
        if hi is not None and value > hi:
            raise HTTPException(
                status_code=400,
                detail=f"Setting '{key}' value {value} is above max ({hi})",
            )

    old_row = await op_store.get_setting(key)
    if old_row is not None:
        old_value = _serialize_existing_value(old_row.get("value"), expected_type)
    else:
        from serving.config.settings import get_settings

        old_value = getattr(get_settings(), key, entry["default"])

    await op_store.set_setting(key, str(value), expected_type, admin_id)
    await _refresh_live_routewise_routers(request, rt)

    ip = get_client_ip(request)
    await log_admin_action(
        op_store,
        ip,
        "routewise_settings.update",
        None,
        {"key": key, "old_value": old_value, "new_value": value},
    )

    return RoutewiseSettingItem(
        key=key,
        value=value,
        value_type=expected_type,
        default_value=entry["default"],
        description=entry["description"],
        min=entry.get("min"),
        max=entry.get("max"),
    )


@router.get("/probes", response_model=ListRoutewiseProbeSamplesResponse)
async def list_routewise_probe_samples_endpoint(
    model_id: str | None = None,
    endpoint_id: str | None = None,
    since_seconds: int = 86_400,
    limit: int = 200,
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ListRoutewiseProbeSamplesResponse:
    """List recent persisted RouteWise active-probe samples."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=max(int(since_seconds), 1))
    rows = await op_store.list_routewise_probe_samples(
        model_id=model_id,
        endpoint_id=endpoint_id,
        since=since,
        newest_first=True,
        limit=max(min(int(limit), 1000), 1),
    )
    return ListRoutewiseProbeSamplesResponse(
        samples=[
            RoutewiseProbeSampleItem(
                model_id=str(row["model_id"]),
                endpoint_id=str(row["endpoint_id"]),
                ttft_ms=(float(row["ttft_ms"]) if row.get("ttft_ms") is not None else None),
                ok=bool(row["ok"]),
                error=row.get("error"),
                checked_at=row["checked_at"],
            )
            for row in rows
        ]
    )


@router.post("/probes/run", response_model=RunRoutewiseProbeResponse)
async def run_routewise_probe_endpoint(
    request: Request,
    payload: RunRoutewiseProbeRequest,
    admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> RunRoutewiseProbeResponse:
    """Manually run RouteWise latency probes against live route candidates."""
    routers = _routewise_routers_for_probe(services, payload.model_id)
    if not routers:
        raise HTTPException(status_code=404, detail="No RouteWise router found")
    results = []
    for router_obj in routers:
        attach_store = getattr(router_obj, "attach_operational_store", None)
        if callable(attach_store):
            attach_store(op_store)
        probe_model_id = payload.model_id
        canonical = getattr(router_obj, "_canonical_model_id", None)
        if callable(canonical) and probe_model_id:
            probe_model_id = canonical(probe_model_id)
        results.extend(
            await router_obj.run_probe_once(
                model_id=probe_model_id,
                endpoint_id=payload.endpoint_id,
                idle_only=payload.idle_only,
            )
        )
    # Audit after probing so failures still return probe diagnostics to the UI.
    await log_admin_action(
        op_store,
        get_client_ip(request),
        "routewise_probes.run",
        None,
        {
            "model_id": payload.model_id,
            "endpoint_id": payload.endpoint_id,
            "idle_only": payload.idle_only,
            "result_count": len(results),
            "admin_id": admin_id,
        },
    )
    return RunRoutewiseProbeResponse(
        results=[
            RoutewiseProbeRunResult(
                model_id=result.model_id,
                endpoint_id=result.endpoint_id,
                ok=result.ok,
                ttft_ms=result.ttft_ms,
                error=result.error,
            )
            for result in results
        ]
    )


@router.get("/decisions", response_model=RoutewiseDecisionsResponse)
async def get_routewise_decisions_endpoint(
    model_id: str,
    range: Literal["24h", "7d", "30d"] = "24h",
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> RoutewiseDecisionsResponse:
    """Aggregate a model's RouteWise routing decisions over a lookback window.

    Scans ``api_logs`` rows for ``model_id`` whose ``metadata`` carries a
    ``routewise`` decision blob within the window implied by ``range`` and
    aggregates them server-side (one GROUP BY per facet). ``selection_share`` and
    each bucket's ``counts`` attribute each request to the endpoint that actually
    served it, so a hedge backup win counts toward its backup endpoint
    (``backup_provider``) rather than the primary ``final_endpoint``.
    ``total_requests`` counts every attributed and unattributed row;
    ``unattributed_requests`` counts rows with no served endpoint (error paths)
    and are excluded from ``selection_share`` and each bucket's ``counts``. A
    bucket appears when it holds at least one routewise row, and its ``hedge``
    breakdown covers every such row. ``hedge_summary`` reports the window hedge
    rate (over all requests) and backup win rate (over hedged requests).

    Args:
        model_id: Canonical model id to aggregate (exact match, required).
        range: Lookback window, one of ``"24h"``, ``"7d"``, ``"30d"``.
        _admin_id: Injected admin identity from the auth dependency.
        db_logger: Injected database logger providing the connection pool.

    Returns:
        The aggregated decision counts, LP status mix, per-endpoint selection
        share, window hedge KPIs, and per-bucket selection and hedge counts.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    window_seconds, bucket_seconds = DECISIONS_RANGE_SECONDS[range]

    async with db_logger.pool.acquire() as conn:
        counts_row = await conn.fetchrow(
            f"""
            SELECT
                COUNT(*) AS total_requests,
                COUNT(*) FILTER (
                    WHERE ({SERVED_ENDPOINT_SQL}) IS NULL
                ) AS unattributed_requests,
                COUNT(*) FILTER (
                    WHERE (metadata->'routewise'->>'hedged') = 'true'
                ) AS hedged,
                COUNT(*) FILTER (
                    WHERE (metadata->'routewise'->>'hedged') = 'true'
                      AND (metadata->'routewise'->>'hedge_winner') = 'backup'
                ) AS backup_won,
                percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY (metadata->'routewise'->>'hedge_delay_ms')::double precision
                ) FILTER (
                    WHERE (metadata->'routewise'->>'hedged') = 'true'
                      AND (metadata->'routewise'->>'hedge_delay_ms') IS NOT NULL
                ) AS median_hedge_delay_ms
            FROM api_logs
            WHERE model_id = $1
              AND timestamp >= NOW() - ($2::int * interval '1 second')
              AND metadata ? 'routewise'
            """,
            model_id,
            window_seconds,
        )

        lp_status_rows = await conn.fetch(
            """
            SELECT
                metadata->'routewise'->>'lp_status' AS lp_status,
                COUNT(*) AS cnt
            FROM api_logs
            WHERE model_id = $1
              AND timestamp >= NOW() - ($2::int * interval '1 second')
              AND metadata ? 'routewise'
              AND (metadata->'routewise'->>'lp_status') IS NOT NULL
            GROUP BY 1
            """,
            model_id,
            window_seconds,
        )

        selection_rows = await conn.fetch(
            f"""
            SELECT
                {SERVED_ENDPOINT_SQL} AS endpoint,
                {SERVED_PROVIDER_TYPE_SQL} AS provider_type,
                COUNT(*) AS cnt
            FROM api_logs
            WHERE model_id = $1
              AND timestamp >= NOW() - ($2::int * interval '1 second')
              AND metadata ? 'routewise'
              AND ({SERVED_ENDPOINT_SQL}) IS NOT NULL
            GROUP BY 1, 2
            ORDER BY cnt DESC, endpoint ASC
            """,
            model_id,
            window_seconds,
        )

        bucket_rows = await conn.fetch(
            f"""
            SELECT
                to_timestamp(
                    floor(extract(epoch FROM timestamp) / $3::int) * $3::int
                ) AS bucket_start,
                {SERVED_ENDPOINT_SQL} AS endpoint,
                COUNT(*) AS cnt
            FROM api_logs
            WHERE model_id = $1
              AND timestamp >= NOW() - ($2::int * interval '1 second')
              AND metadata ? 'routewise'
              AND ({SERVED_ENDPOINT_SQL}) IS NOT NULL
            GROUP BY 1, 2
            ORDER BY bucket_start ASC
            """,
            model_id,
            window_seconds,
            bucket_seconds,
        )

        # Hedge breakdown over ALL routewise rows in each bucket. Drives bucket
        # inclusion (>= 1 routewise row) so buckets holding only unattributed
        # rows still surface with an empty ``counts`` map.
        hedge_bucket_rows = await conn.fetch(
            """
            SELECT
                to_timestamp(
                    floor(extract(epoch FROM timestamp) / $3::int) * $3::int
                ) AS bucket_start,
                COUNT(*) FILTER (
                    WHERE (metadata->'routewise'->>'hedged') IS DISTINCT FROM 'true'
                ) AS not_hedged,
                COUNT(*) FILTER (
                    WHERE (metadata->'routewise'->>'hedged') = 'true'
                      AND (metadata->'routewise'->>'hedge_winner') IS DISTINCT FROM 'backup'
                ) AS hedged_primary_won,
                COUNT(*) FILTER (
                    WHERE (metadata->'routewise'->>'hedged') = 'true'
                      AND (metadata->'routewise'->>'hedge_winner') = 'backup'
                ) AS hedged_backup_won
            FROM api_logs
            WHERE model_id = $1
              AND timestamp >= NOW() - ($2::int * interval '1 second')
              AND metadata ? 'routewise'
            GROUP BY 1
            ORDER BY bucket_start ASC
            """,
            model_id,
            window_seconds,
            bucket_seconds,
        )

    total_requests = int(counts_row["total_requests"] or 0) if counts_row else 0
    unattributed_requests = int(counts_row["unattributed_requests"] or 0) if counts_row else 0
    hedged = int(counts_row.get("hedged") or 0) if counts_row else 0
    backup_won = int(counts_row.get("backup_won") or 0) if counts_row else 0
    median_delay_raw = counts_row.get("median_hedge_delay_ms") if counts_row else None
    median_hedge_delay_ms = float(median_delay_raw) if median_delay_raw is not None else None

    hedge_summary = RoutewiseHedgeSummary(
        hedged=hedged,
        hedge_rate=(hedged / total_requests) if total_requests else 0.0,
        backup_won=backup_won,
        backup_win_rate=(backup_won / hedged) if hedged else 0.0,
        median_hedge_delay_ms=median_hedge_delay_ms,
    )

    lp_status_counts = {str(row["lp_status"]): int(row["cnt"] or 0) for row in lp_status_rows}

    selection_share = [
        RoutewiseSelectionShareItem(
            endpoint=str(row["endpoint"]),
            provider_type=row["provider_type"],
            count=int(row["cnt"] or 0),
        )
        for row in selection_rows
    ]

    # Attributed per-endpoint counts keyed by bucket_start; a bucket may be absent
    # here yet still appear below when it holds only unattributed routewise rows.
    counts_by_start: dict[str, dict[str, int]] = {}
    for row in bucket_rows:
        start_key = row["bucket_start"].isoformat()
        counts_by_start.setdefault(start_key, {})[str(row["endpoint"])] = int(row["cnt"] or 0)

    # The hedge-bucket rows cover every routewise row and arrive ordered by
    # bucket_start ASC, so they set both bucket inclusion and ascending order.
    buckets = [
        RoutewiseDecisionBucket(
            bucket_start=row["bucket_start"].isoformat(),
            counts=counts_by_start.get(row["bucket_start"].isoformat(), {}),
            hedge=RoutewiseDecisionBucketHedge(
                not_hedged=int(row["not_hedged"] or 0),
                hedged_primary_won=int(row["hedged_primary_won"] or 0),
                hedged_backup_won=int(row["hedged_backup_won"] or 0),
            ),
        )
        for row in hedge_bucket_rows
    ]

    return RoutewiseDecisionsResponse(
        model_id=model_id,
        range=range,
        bucket_seconds=bucket_seconds,
        total_requests=total_requests,
        unattributed_requests=unattributed_requests,
        lp_status_counts=lp_status_counts,
        selection_share=selection_share,
        hedge_summary=hedge_summary,
        buckets=buckets,
    )
