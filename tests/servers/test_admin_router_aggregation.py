"""Sanity test for the admin router package split.

Verifies that __init__.py correctly aggregates all sub-routers and
that no route was dropped or duplicated during the split.
"""

from __future__ import annotations

from fastapi import APIRouter

from serving.servers.routers import admin


def test_admin_module_exports_router() -> None:
    """`from serving.servers.routers import admin` exposes a single APIRouter."""
    assert hasattr(admin, "router"), "admin.router missing"
    assert isinstance(admin.router, APIRouter), "admin.router is not an APIRouter"


def test_admin_router_has_expected_route_count() -> None:
    """Snapshot route count to catch accidental loss/dup during future edits.

    Bump this number deliberately when adding/removing admin routes.
    """
    expected = 56  # includes role-quota, provider-key, visibility, routewise, and routing-weight routes
    routes = [r for r in admin.router.routes if hasattr(r, "path") and r.path.startswith("/admin")]
    assert len(routes) == expected, (
        f"admin route count drifted: expected {expected}, got {len(routes)}"
    )


def test_admin_routes_have_no_path_collisions() -> None:
    """Each (method, path) tuple appears at most once."""
    seen: set[tuple[str, str]] = set()
    for r in admin.router.routes:
        if not hasattr(r, "path") or not hasattr(r, "methods"):
            continue
        for method in r.methods:
            key = (method, r.path)
            assert key not in seen, f"duplicate route: {method} {r.path}"
            seen.add(key)
