"""Admin router package.

Aggregates domain-focused sub-routers (alerts, analytics,
api_keys, broadcast, export, login_events, metrics, model_concurrency,
model_visibility, providers, settings, signup_domains, site_updates, stats,
usage_insights, users) into a single ``router``
exported at this level. Callers use
``from serving.servers.routers import admin`` unchanged.
"""

from fastapi import APIRouter

from serving.servers.routers.admin import (
    alerts,
    analytics,
    api_keys,
    broadcast,
    export,
    login_events,
    metrics,
    model_concurrency,
    model_visibility,
    provider_definitions,
    provider_keys,
    provider_routes,
    providers,
    quota,
    routing_weights,
    settings,
    signup_domains,
    site_updates,
    stats,
    usage_insights,
    users,
)

try:
    from serving.servers.routers.admin import routewise
except ImportError:  # optional RouteWise extra not installed
    routewise = None

from serving.servers.routers.admin.metrics import _decode_throughput_tps

router = APIRouter()
router.include_router(alerts.router)
router.include_router(analytics.router)
router.include_router(api_keys.router)
router.include_router(broadcast.router)
router.include_router(export.router)
router.include_router(login_events.router)
router.include_router(metrics.router)
router.include_router(model_concurrency.router)
router.include_router(model_visibility.router)
router.include_router(provider_definitions.router)
router.include_router(provider_keys.router)
router.include_router(provider_routes.router)
router.include_router(providers.router)
router.include_router(quota.router)
if routewise is not None:
    router.include_router(routewise.router)
router.include_router(routing_weights.router)
router.include_router(settings.router)
router.include_router(signup_domains.router)
router.include_router(site_updates.router)
router.include_router(stats.router)
router.include_router(usage_insights.router)
router.include_router(users.router)

__all__ = ["_decode_throughput_tps", "router"]
