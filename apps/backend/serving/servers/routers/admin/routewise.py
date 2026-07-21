"""Dedicated admin Routewise runtime settings endpoints."""

from __future__ import annotations

import datetime as dt
import math
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request

from routing.routewise.router import RouteWiseRouter
from serving.config.routewise_model_settings import (
    ROUTEWISE_SETTING_KEYS,
    ResolvedRouteWiseSetting,
    RouteWiseSettingsResolver,
    apply_routewise_settings_to_router,
    model_routewise_setting_key,
)
from serving.config.runtime_settings import (
    RUNTIME_SETTINGS_REGISTRY,
    RuntimeSettings,
    get_runtime_settings,
)
from serving.schemas_admin import (
    ListRoutewiseModelSettingsResponse,
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
    AppServices,
    get_db_logger,
    get_operational_store,
    get_services,
    model_router_transition_lock,
    verify_admin_access,
)
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/admin/routewise")
logger = get_logger(__name__)

ROUTEWISE_KEYS = ROUTEWISE_SETTING_KEYS

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


def _routewise_setting_entry(key: str) -> dict[str, Any]:
    if key not in ROUTEWISE_KEYS:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")
    entry = RUNTIME_SETTINGS_REGISTRY.get(key)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")
    return entry


def _validate_routewise_setting_value(key: str, value: Any) -> dict[str, Any]:
    entry = _routewise_setting_entry(key)
    expected_type = entry["type"]
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

    if expected_type == "float":
        try:
            is_finite = math.isfinite(float(value))
        except (OverflowError, TypeError, ValueError):
            is_finite = False
        if not is_finite:
            raise HTTPException(
                status_code=400,
                detail=f"Setting '{key}' expects a finite numeric value",
            )

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
    return entry


async def _log_admin_action_best_effort(*args: Any, **kwargs: Any) -> None:
    """Keep a committed setting successful when its audit sink is unavailable."""
    try:
        await log_admin_action(*args, **kwargs)
    except Exception:
        logger.exception("Failed to write RouteWise settings audit log")


async def _routewise_settings_resolver(
    services: AppServices,
    op_store: Any,
    rt: RuntimeSettings,
) -> RouteWiseSettingsResolver:
    registry = services.model_router_registry
    if registry is None:
        raise HTTPException(status_code=503, detail="Model router registry not initialized")

    resolver = services.routewise_settings_resolver
    if resolver is None:
        resolver = RouteWiseSettingsResolver(op_store, rt, registry)
        await resolver.load_all()
        services.routewise_settings_resolver = resolver
    return resolver


def _canonical_model_id_or_404(
    resolver: RouteWiseSettingsResolver,
    model_id: str,
) -> str:
    try:
        return resolver.canonical_model_id(model_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc


def _resolved_setting_item(resolved: ResolvedRouteWiseSetting) -> RoutewiseSettingItem:
    entry = RUNTIME_SETTINGS_REGISTRY[resolved.key]
    return RoutewiseSettingItem(
        key=resolved.key,
        value=resolved.value,
        value_type=entry["type"],
        default_value=resolved.fallback_value,
        source=resolved.source,
        overridden=resolved.source == "runtime_override",
        description=entry["description"],
        min=entry.get("min"),
        max=entry.get("max"),
    )


def _routewise_routers(
    services: AppServices,
    model_id: str | None,
) -> list[RouteWiseRouter]:
    """Return unique live RouteWise routers, optionally scoped to one model."""
    registry = services.model_router_registry
    if registry is None:
        return []
    model_ids = (
        [model_id]
        if model_id
        else list(
            dict.fromkeys(
                [
                    *registry.configured_model_ids(),
                    *registry.registered_models(),
                ]
            )
        )
    )
    seen: set[int] = set()
    routers: list[RouteWiseRouter] = []
    for active_model_id in model_ids:
        if registry.get_router_name(active_model_id) != "routewise":
            continue
        router_obj = registry.get_router(active_model_id)
        if not isinstance(router_obj, RouteWiseRouter):
            raise TypeError(
                "routewise strategy returned "
                f"{type(router_obj).__name__} for model {active_model_id!r}"
            )
        if id(router_obj) in seen:
            continue
        routers.append(router_obj)
        seen.add(id(router_obj))
    return routers


async def _refresh_live_routewise_routers(
    services: AppServices,
    rt: RuntimeSettings,
    op_store: Any,
) -> None:
    """Re-resolve every live model without overriding scoped or YAML values."""
    registry = services.model_router_registry
    if registry is None:
        return
    resolver = await _routewise_settings_resolver(services, op_store, rt)
    await resolver.load_all()

    model_ids = [*registry.configured_model_ids(), *registry.registered_models()]
    seen: set[str] = set()
    for model_id in model_ids:
        canonical_model_id = registry.canonical_model_id(model_id)
        if canonical_model_id in seen:
            continue
        seen.add(canonical_model_id)
        async with model_router_transition_lock(services, canonical_model_id):
            router_obj = registry.get_cached_router(canonical_model_id)
            if not isinstance(router_obj, RouteWiseRouter):
                continue
            await apply_routewise_settings_to_router(
                resolver,
                registry,
                canonical_model_id,
                router_obj,
                refresh_probe_task=True,
            )


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
    services: AppServices = Depends(get_services),
    op_store=Depends(get_operational_store),
    rt: RuntimeSettings | None = Depends(get_runtime_settings),
) -> RoutewiseSettingItem:
    """Update a single curated Routewise runtime setting by key."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    rt = _require_runtime_settings(rt)

    value = payload.value
    entry = _validate_routewise_setting_value(key, value)
    expected_type = entry["type"]

    old_row = await op_store.get_setting(key)
    if old_row is not None:
        old_value = _serialize_existing_value(old_row.get("value"), expected_type)
    else:
        from serving.config.settings import get_settings

        old_value = getattr(get_settings(), key, entry["default"])

    await op_store.set_setting(key, str(value), expected_type, admin_id)
    try:
        await _refresh_live_routewise_routers(services, rt, op_store)
    except Exception:
        # The database is authoritative for this compatibility default. The
        # background reconciler retries every worker, so a transient live apply
        # failure must not report the committed write as rejected.
        logger.exception(
            "Failed to immediately apply legacy global RouteWise setting key=%s; "
            "background reconciliation will retry",
            key,
        )

    ip = get_client_ip(request)
    await _log_admin_action_best_effort(
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


@router.get(
    "/model-settings",
    response_model=ListRoutewiseModelSettingsResponse,
)
async def list_model_routewise_settings_endpoint(
    model_id: str,
    _admin_id: str = Depends(verify_admin_access),
    services: AppServices = Depends(get_services),
    op_store=Depends(get_operational_store),
    rt: RuntimeSettings | None = Depends(get_runtime_settings),
) -> ListRoutewiseModelSettingsResponse:
    """List effective RouteWise settings for one known canonical model."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    rt = _require_runtime_settings(rt)
    resolver = await _routewise_settings_resolver(services, op_store, rt)
    canonical_model_id = _canonical_model_id_or_404(resolver, model_id)
    async with model_router_transition_lock(services, canonical_model_id):
        canonical_model_id = _canonical_model_id_or_404(resolver, model_id)
        resolved = await resolver.resolve_model(canonical_model_id)
    return ListRoutewiseModelSettingsResponse(
        model_id=canonical_model_id,
        settings=[_resolved_setting_item(resolved[key]) for key in ROUTEWISE_KEYS],
    )


@router.patch(
    "/model-settings/{key}",
    response_model=RoutewiseSettingItem,
)
async def update_model_routewise_setting_endpoint(
    request: Request,
    key: str,
    payload: UpdateSettingRequest,
    model_id: str,
    admin_id: str = Depends(verify_admin_access),
    services: AppServices = Depends(get_services),
    op_store=Depends(get_operational_store),
    rt: RuntimeSettings | None = Depends(get_runtime_settings),
) -> RoutewiseSettingItem:
    """Set a persisted RouteWise override owned by one canonical model."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    rt = _require_runtime_settings(rt)
    value = payload.value
    entry = _validate_routewise_setting_value(key, value)
    resolver = await _routewise_settings_resolver(services, op_store, rt)
    registry = services.model_router_registry
    if registry is None:  # guarded by _routewise_settings_resolver
        raise HTTPException(status_code=503, detail="Model router registry not initialized")
    canonical_model_id = _canonical_model_id_or_404(resolver, model_id)

    async with model_router_transition_lock(services, canonical_model_id):
        # A runtime model can disappear while this request waits for the same
        # transition lock behind final-route deletion.
        canonical_model_id = _canonical_model_id_or_404(resolver, model_id)
        previous = await resolver.get_resolved(canonical_model_id, key)
        setting_key = model_routewise_setting_key(key, canonical_model_id)
        await op_store.set_setting(
            setting_key,
            str(value),
            entry["type"],
            admin_id,
        )
        resolver.set_override(canonical_model_id, key, value)
        resolved = await resolver.get_resolved(canonical_model_id, key)
        router_obj = registry.get_cached_router(canonical_model_id)
        if isinstance(router_obj, RouteWiseRouter):
            try:
                await apply_routewise_settings_to_router(
                    resolver,
                    registry,
                    canonical_model_id,
                    router_obj,
                    refresh_probe_task=True,
                )
            except Exception:
                # The persisted value is authoritative. Rolling it back here
                # would race a newer write made by another worker and could
                # overwrite that worker's value. The reconciler retries this
                # router from the durable snapshot.
                logger.exception(
                    "Failed to immediately apply RouteWise model setting "
                    "model=%s key=%s; background reconciliation will retry",
                    canonical_model_id,
                    key,
                )

    await _log_admin_action_best_effort(
        op_store,
        get_client_ip(request),
        "routewise_model_settings.update",
        canonical_model_id,
        {
            "key": key,
            "old_value": previous.value,
            "new_value": resolved.value,
            "source": resolved.source,
        },
    )
    return _resolved_setting_item(resolved)


@router.delete(
    "/model-settings/{key}",
    response_model=RoutewiseSettingItem,
)
async def reset_model_routewise_setting_endpoint(
    request: Request,
    key: str,
    model_id: str,
    admin_id: str = Depends(verify_admin_access),
    services: AppServices = Depends(get_services),
    op_store=Depends(get_operational_store),
    rt: RuntimeSettings | None = Depends(get_runtime_settings),
) -> RoutewiseSettingItem:
    """Idempotently remove a model override and return its inherited value."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    rt = _require_runtime_settings(rt)
    _routewise_setting_entry(key)
    resolver = await _routewise_settings_resolver(services, op_store, rt)
    registry = services.model_router_registry
    if registry is None:  # guarded by _routewise_settings_resolver
        raise HTTPException(status_code=503, detail="Model router registry not initialized")
    canonical_model_id = _canonical_model_id_or_404(resolver, model_id)

    async with model_router_transition_lock(services, canonical_model_id):
        canonical_model_id = _canonical_model_id_or_404(resolver, model_id)
        previous = await resolver.get_resolved(canonical_model_id, key)
        setting_key = model_routewise_setting_key(key, canonical_model_id)
        await op_store.delete_setting(setting_key)
        resolver.clear_override(canonical_model_id, key)
        resolved = await resolver.get_resolved(canonical_model_id, key)
        router_obj = registry.get_cached_router(canonical_model_id)
        if isinstance(router_obj, RouteWiseRouter):
            try:
                await apply_routewise_settings_to_router(
                    resolver,
                    registry,
                    canonical_model_id,
                    router_obj,
                    refresh_probe_task=True,
                )
            except Exception:
                logger.exception(
                    "Failed to immediately apply RouteWise model setting reset "
                    "model=%s key=%s; background reconciliation will retry",
                    canonical_model_id,
                    key,
                )

    await _log_admin_action_best_effort(
        op_store,
        get_client_ip(request),
        "routewise_model_settings.reset",
        canonical_model_id,
        {
            "key": key,
            "old_value": previous.value,
            "new_value": resolved.value,
            "source": resolved.source,
            "admin_id": admin_id,
        },
    )
    return _resolved_setting_item(resolved)


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
    services: AppServices = Depends(get_services),
    op_store=Depends(get_operational_store),
) -> RunRoutewiseProbeResponse:
    """Manually run RouteWise latency probes against live route candidates."""
    routers = _routewise_routers(services, payload.model_id)
    if not routers:
        raise HTTPException(status_code=404, detail="No RouteWise router found")
    results = []
    for router_obj in routers:
        router_obj.attach_operational_store(op_store)
        probe_model_id = payload.model_id
        if probe_model_id:
            probe_model_id = router_obj.canonical_model_id(probe_model_id)
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
