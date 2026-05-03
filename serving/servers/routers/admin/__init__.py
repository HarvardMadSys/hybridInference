"""Admin router package.

This file aggregates per-domain sub-routers. During the in-progress
split (see docs/superpowers/specs/2026-05-03-admin-router-split-design.md)
the legacy single-file router is exposed here so callers continue to
work via `from serving.servers.routers import admin`.
"""

from fastapi import APIRouter

from serving.servers.routers.admin import api_keys, stats
from serving.servers.routers.admin._admin_legacy import (
    _decode_throughput_tps,
    router as _legacy_router,
)

router = APIRouter()
router.include_router(api_keys.router)
router.include_router(stats.router)
router.include_router(_legacy_router)

__all__ = ["_decode_throughput_tps", "router"]
