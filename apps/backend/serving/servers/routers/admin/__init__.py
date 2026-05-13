"""Admin router package.

Aggregates domain-focused sub-routers (analytics, api_keys, broadcast,
export, login_events, metrics, providers, settings, signup_domains, stats,
users) into a single ``router`` exported at this level. Callers use
``from serving.servers.routers import admin`` unchanged.
"""

from fastapi import APIRouter

from serving.servers.routers.admin import (
    analytics,
    api_keys,
    broadcast,
    export,
    login_events,
    metrics,
    model_visibility,
    provider_keys,
    providers,
    quota,
    routewise,
    routing_weights,
    settings,
    signup_domains,
    stats,
    users,
)
from serving.servers.routers.admin.metrics import _decode_throughput_tps

router = APIRouter()
router.include_router(analytics.router)
router.include_router(api_keys.router)
router.include_router(broadcast.router)
router.include_router(export.router)
router.include_router(login_events.router)
router.include_router(metrics.router)
router.include_router(model_visibility.router)
router.include_router(provider_keys.router)
router.include_router(providers.router)
router.include_router(quota.router)
router.include_router(routewise.router)
router.include_router(routing_weights.router)
router.include_router(settings.router)
router.include_router(signup_domains.router)
router.include_router(stats.router)
router.include_router(users.router)

__all__ = ["_decode_throughput_tps", "router"]
