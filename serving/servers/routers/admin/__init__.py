"""Admin router package.

This file aggregates per-domain sub-routers. During the in-progress
split (see docs/superpowers/specs/2026-05-03-admin-router-split-design.md)
the legacy single-file router is exposed here so callers continue to
work via `from serving.servers.routers import admin`.
"""

from serving.servers.routers.admin._admin_legacy import _decode_throughput_tps, router

__all__ = ["_decode_throughput_tps", "router"]
