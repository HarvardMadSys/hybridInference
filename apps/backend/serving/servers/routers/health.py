"""Health, root, and routing info endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse

from serving.config.distribution import get_distribution_config_comparison_state
from serving.config.settings import get_settings, has_role
from serving.observability.alerts import AlertSeverity, alert_slack
from serving.servers.auth import is_user_auth_enabled, optional_verify_api_key
from serving.servers.deps import get_log_store, get_operational_store, get_router, get_services
from serving.servers.routers.capabilities import distribution_capability_mismatches

router = APIRouter()


def _prometheus_label(value: str) -> str:
    """Escape one bounded metric-label value."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> PlainTextResponse:
    """Expose value-free distribution comparison gauges for scraping."""
    lines = [
        "# HELP hybridinference_distribution_config_mismatch "
        "Whether a distribution candidate differs or cannot be compared.",
        "# TYPE hybridinference_distribution_config_mismatch gauge",
    ]
    for resource, state in get_distribution_config_comparison_state().items():
        labels = {
            "resource": resource,
            "selector": str(state["selector"]),
            "source": str(state["source"]),
            "status": str(state["status"]),
        }
        rendered_labels = ",".join(
            f'{name}="{_prometheus_label(value)}"' for name, value in labels.items()
        )
        lines.append(
            "hybridinference_distribution_config_mismatch"
            f"{{{rendered_labels}}} {int(state['mismatch'])}"
        )
    return PlainTextResponse("\n".join(lines) + "\n")


def _route_is_published(route: Any) -> bool:
    """Return whether a route may be exposed by serving discovery endpoints."""
    return bool(getattr(route, "published", True))


async def _test_store_health(op_store: Any, log_store: Any) -> dict[str, Any]:
    """Actively test database connection via store health checks.

    Returns:
        Dict with per-store status:
        ``{"operational_store": {...}, "log_store": {...}, "healthy": bool}``
    """
    from serving.storage.cache import CachedOperationalStore
    from serving.storage.postgres_log import PostgresLogStore
    from serving.storage.postgres_operational import PostgresOperationalStore

    result: dict[str, Any] = {"healthy": False, "database_configured": False}

    # Operational store
    op_status: dict[str, Any] = {"status": "unavailable", "backend": "none"}
    op_error: str | None = None
    if op_store:
        result["database_configured"] = True
        # Resolve the underlying backend through CachedOperationalStore
        inner = getattr(op_store, "_store", op_store)
        if isinstance(inner, PostgresOperationalStore):
            op_status["backend"] = "postgres"
        try:
            op_status["status"] = "ok" if await op_store.health_check() else "error"
        except Exception as exc:
            op_status["status"] = "error"
            op_error = str(exc)
        if isinstance(op_store, CachedOperationalStore):
            op_status["cache"] = "in_memory"
    result["operational_store"] = op_status

    # Log store
    log_status: dict[str, Any] = {"status": "unavailable", "backend": "none"}
    log_error: str | None = None
    if log_store:
        result["database_configured"] = True
        log_status["backend"] = "postgres" if isinstance(log_store, PostgresLogStore) else "unknown"
        try:
            log_status["status"] = "ok" if await log_store.health_check() else "error"
        except Exception as exc:
            log_status["status"] = "error"
            log_error = str(exc)
    result["log_store"] = log_status

    # Track both OR (any store up) and AND (all configured stores up).
    # `healthy` (OR) preserves backward-compatible /health behavior so the
    # Docker HEALTHCHECK probe in Dockerfile.backend doesn't restart the
    # container on transient log_store hiccups while the operational store
    # is still serving auth.
    # `all_healthy` (AND) is exposed for stricter readiness probes
    # (see /health/ready).
    if not result["database_configured"]:
        result["healthy"] = True
        result["all_healthy"] = True
    else:
        configured_statuses = [
            s["status"] for s in (op_status, log_status) if s["status"] != "unavailable"
        ]
        result["healthy"] = any(s == "ok" for s in configured_statuses)
        result["all_healthy"] = bool(configured_statuses) and all(
            s == "ok" for s in configured_statuses
        )
    # Fire DB disconnect alert when a configured store is unhealthy. dedupe_key
    # ensures we only alert once per cooldown per store kind.
    if op_store and op_status["status"] == "error":
        await alert_slack(
            AlertSeverity.CRITICAL,
            "Database disconnected",
            {
                "db_kind": op_status.get("backend", "operational"),
                "store": "operational_store",
                "error": (op_error or "health_check returned False")[:500],
            },
            dedupe_key=f"db_disconnect:{op_status.get('backend', 'operational')}",
            cooldown_sec=300,
        )
    if log_store and log_status["status"] == "error":
        await alert_slack(
            AlertSeverity.CRITICAL,
            "Database disconnected",
            {
                "db_kind": log_status.get("backend", "log"),
                "store": "log_store",
                "error": (log_error or "health_check returned False")[:500],
            },
            dedupe_key=f"db_disconnect:{log_status.get('backend', 'log')}_log",
            cooldown_sec=300,
        )

    return result


@router.get("/")
async def root() -> dict[str, Any]:
    """Root endpoint showing API information and static metadata."""
    return {
        "message": "OpenRouter-Compatible API Server",
        "version": "2.0.0",
        "features": [
            "Load balancing",
            "Automatic fallback",
            "Database logging",
        ],
        "endpoints": {
            "/v1/chat/completions": "Chat completions endpoint",
            "/completion": "Single-shot completion endpoint (alias)",
            "/models": "List available models (OpenRouter schema)",
            "/v1/models": "List available models",
            "/openrouter/models": "OpenRouter format models list",
            "/routing": "Show routing configuration",
            "/admin/stats": "API usage statistics (admin auth required)",
            "/health": "Health check",
        },
    }


@router.get("/health")
async def health(
    response: Response,
    router_exec=Depends(get_router),
    op_store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
) -> dict[str, Any]:
    """Liveness check endpoint with active database connection test.

    Returns 200 when at least one configured store is reachable; surfaces
    ``status: "degraded"`` in the body when a configured store is down but
    the service can still serve traffic. Returns 503 only when every
    configured store is unreachable (full DB outage).

    The 200-with-degraded-body shape is intentional: the Docker
    HEALTHCHECK in Dockerfile.backend uses ``curl -f /health`` and the
    frontend's docker-compose ``depends_on.backend.condition:
    service_healthy`` would tear down healthy backends on transient
    log_store hiccups if we returned 503 for partial degradation. Strict
    readiness consumers should use ``/health/ready`` instead, which does
    AND-logic and returns 503 unless every configured store is up.
    """
    routes_count = sum(_route_is_published(route) for route in router_exec.routes.values())

    store_health = await _test_store_health(op_store, log_store)
    db_connected = store_health["healthy"]

    if not db_connected:
        if not store_health["database_configured"]:
            # No database configured — service is healthy without a DB
            return {
                "status": "healthy",
                "routes_configured": routes_count,
                "database_configured": False,
                "database_connected": False,
            }
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "unhealthy",
            "reason": "database_disconnected",
            "routes_configured": routes_count,
            "database_configured": True,
            "database_connected": False,
            "stores": {
                "operational_store": store_health["operational_store"],
                "log_store": store_health["log_store"],
            },
        }

    # At least one store is up. If any configured store is down, surface
    # `degraded` in the body without flipping the status code.
    body_status = "healthy" if store_health["all_healthy"] else "degraded"
    return {
        "status": body_status,
        "routes_configured": routes_count,
        "database_configured": True,
        "database_connected": True,
        "stores": {
            "operational_store": store_health["operational_store"],
            "log_store": store_health["log_store"],
        },
    }


@router.get("/health/ready")
async def health_ready(
    request: Request,
    response: Response,
    op_store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
) -> dict[str, Any]:
    """Strict readiness probe: every configured store must be reachable.

    Returns 200 only when all configured stores pass health_check();
    returns 503 if any configured store is unreachable. Intended for
    alertmanager / kubelet readiness consumers that want to drain the
    instance from a load balancer on partial-DB degradation. Distinct
    from ``/health`` (liveness), which stays 200 with ``status:
    "degraded"`` to avoid container restart loops.
    """
    store_health = await _test_store_health(op_store, log_store)
    capability_mismatches = await distribution_capability_mismatches(request)
    required_capabilities_unavailable = sorted(
        capability_id
        for capability_id, (expected, effective) in capability_mismatches.items()
        if expected and not effective
    )
    unexpectedly_enabled_capabilities = sorted(
        capability_id
        for capability_id, (expected, effective) in capability_mismatches.items()
        if not expected and effective
    )
    fail_required_capabilities = bool(
        get_settings().distribution_config_required and capability_mismatches
    )

    if store_health["all_healthy"] and not fail_required_capabilities:
        return {
            "status": "ready",
            "database_configured": store_health["database_configured"],
            "stores": {
                "operational_store": store_health["operational_store"],
                "log_store": store_health["log_store"],
            },
        }

    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    if required_capabilities_unavailable and not unexpectedly_enabled_capabilities:
        reason = "required_distribution_capability_unavailable"
    elif fail_required_capabilities:
        reason = "distribution_capability_expectation_mismatch"
    else:
        reason = "store_degraded"
    return {
        "status": "not_ready",
        "reason": reason,
        "database_configured": store_health["database_configured"],
        **(
            {
                "unavailable_capabilities": required_capabilities_unavailable,
                "unexpectedly_enabled_capabilities": unexpectedly_enabled_capabilities,
            }
            if fail_required_capabilities
            else {}
        ),
        "stores": {
            "operational_store": store_health["operational_store"],
            "log_store": store_health["log_store"],
        },
    }


@router.get("/health/deep")
async def deep_health(
    response: Response,
    router_exec=Depends(get_router),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
) -> dict[str, Any]:
    """Deep health check with provider/circuit info.

    Performs active database connection test and returns detailed system status.
    """
    routes_count = sum(_route_is_published(route) for route in router_exec.routes.values())

    store_health = await _test_store_health(op_store, log_store)
    db_connected = store_health["healthy"]

    provider_status = (
        router_exec.get_provider_status() if hasattr(router_exec, "get_provider_status") else {}
    )

    overall = "healthy"
    if not db_connected and store_health["database_configured"]:
        overall = "unhealthy"
    elif db_connected:
        for _p, s in provider_status.items():
            if s.get("circuit_state") == "open" or (
                s.get("availability") is not None and s.get("availability") < 0.9
            ):
                overall = "degraded"
                break

    if overall == "unhealthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": overall,
        "routes_configured": routes_count,
        "database_connected": db_connected,
        "stores": {
            "operational_store": store_health["operational_store"],
            "log_store": store_health["log_store"],
        },
        "providers": provider_status,
    }


@router.get("/health/model-activity")
async def model_activity(
    window: int = Query(default=10, ge=1, le=60),
    user_ctx: dict[str, Any] | None = Depends(optional_verify_api_key),
    log_store=Depends(get_log_store),
) -> dict[str, Any]:
    """Per-model, per-provider traffic activity over a recent window.

    Internal+ endpoint used by the prober to decide whether to skip
    synthetic probes when real user traffic provides sufficient signal.
    Requires USER_AUTH_ENABLED=1 — always returns 403 in auth-disabled
    deployments to prevent unintentional exposure of traffic stats.
    """
    if not is_user_auth_enabled():
        raise HTTPException(status_code=403, detail="Requires USER_AUTH_ENABLED=1")

    user_role = (user_ctx or {}).get("role", "free")
    if not user_ctx or not has_role(user_role, "internal"):
        raise HTTPException(status_code=403, detail="Internal access required")
    if not log_store:
        raise HTTPException(status_code=503, detail="Database not available")
    routes = await log_store.get_model_activity(window_minutes=window)
    return {"window_minutes": window, "routes": routes}


@router.get("/routing")
async def get_routing(
    router_exec=Depends(get_router),
    services=Depends(get_services),
) -> dict[str, Any]:
    """Show current routing configuration and manager status if present."""
    routing_info: dict[str, Any] = {}
    for model_id, route in router_exec.routes.items():
        if not _route_is_published(route):
            continue
        routing_info[model_id] = [
            {
                "provider": adapter.config.provider,
                "base_url": adapter.config.base_url,
                "weight": f"{weight * 100:.0f}%",
            }
            for adapter, weight in route.adapters
        ]

    response: dict[str, Any] = {
        "routes": routing_info,
        "description": "Weight distribution for each model. Requests are randomly distributed based on weights.",
    }

    if services.routing_manager:
        response["manager_status"] = services.routing_manager.get_status()

    return response
