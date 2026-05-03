"""Admin router package.

This file aggregates per-domain sub-routers. During the in-progress
split (see docs/superpowers/specs/2026-05-03-admin-router-split-design.md)
the legacy single-file router is exposed here so callers continue to
work via `from serving.servers.routers import admin`.
"""

from fastapi import APIRouter

from serving.servers.routers.admin import (
    analytics,
    api_keys,
    broadcast,
    export,
    metrics,
    stats,
    users,
)
from serving.servers.routers.admin._admin_legacy import router as _legacy_router
from serving.servers.routers.admin.metrics import _decode_throughput_tps

router = APIRouter()
router.include_router(analytics.router)
router.include_router(api_keys.router)
router.include_router(broadcast.router)
router.include_router(export.router)
router.include_router(metrics.router)
router.include_router(stats.router)
router.include_router(users.router)
router.include_router(_legacy_router)

__all__ = ["_decode_throughput_tps", "router"]
