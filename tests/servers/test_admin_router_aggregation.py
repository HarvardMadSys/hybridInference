"""Sanity test for the admin router package split.

Verifies that __init__.py correctly aggregates all sub-routers and
that no route was dropped or duplicated during the split.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.routing import APIRoute

from serving.servers.routers import admin


def _admin_api_routes() -> list[APIRoute]:
    """Flatten ``admin.router`` to its leaf ``APIRoute`` objects.

    FastAPI >= 0.137 includes sub-routers lazily: ``admin.router.routes`` holds
    ``_IncludedRouter`` wrappers (with no flat ``path``) rather than the
    flattened routes earlier versions produced. Recurse through each wrapper's
    ``original_router`` to recover the real routes. A loud failure here on a
    future FastAPI change is intentional — it signals this introspection needs
    revisiting rather than silently counting zero routes.
    """

    def walk(router: APIRouter):
        for route in router.routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                yield from walk(included)
            elif isinstance(route, APIRoute):
                yield route

    return list(walk(admin.router))


def test_admin_module_exports_router() -> None:
    """`from serving.servers.routers import admin` exposes a single APIRouter."""
    assert hasattr(admin, "router"), "admin.router missing"
    assert isinstance(admin.router, APIRouter), "admin.router is not an APIRouter"


def test_admin_router_has_expected_route_count() -> None:
    """Snapshot route count to catch accidental loss/dup during future edits.

    Bump this number deliberately when adding/removing admin routes.
    """
    expected = 107  # includes role-quota, provider-key verification/probe
    # (incl. disable/enable/enable-env and the by-ref disable/enable pair used
    # by the quota dashboard), visibility, concurrency,
    # global + per-model routewise settings/probes/decisions, routing-weight,
    # recent-requests/clear-errors, provider-route model creation/update/delete,
    # alert-snooze GET/POST/DELETE,
    # site-updates CRUD routes (regenerate-api-key removed in #733),
    # users/turn-averages, users/ask-question-fractions, usage-insights/analyze,
    # usage-insights/settings GET/PUT, and the per-user + bulk users
    # automation-score routes, plus provider-observability,
    # plus provider-definitions list/verify/create/update/delete,
    # plus provider availability list + disable/enable toggle,
    # plus the geo-temporal analytics endpoint,
    # 110 -> 107 at H4: agent runner-hosts list/set-active/forget went to
    # the cloud agent's repository with the pool they administer.
    routes = [r for r in _admin_api_routes() if r.path.startswith("/admin")]
    assert len(routes) == expected, (
        f"admin route count drifted: expected {expected}, got {len(routes)}"
    )


def test_admin_routes_have_no_path_collisions() -> None:
    """Each (method, path) tuple appears at most once."""
    seen: set[tuple[str, str]] = set()
    for r in _admin_api_routes():
        for method in r.methods:
            key = (method, r.path)
            assert key not in seen, f"duplicate route: {method} {r.path}"
            seen.add(key)
