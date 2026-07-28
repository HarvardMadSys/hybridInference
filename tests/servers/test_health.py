"""Tests for /health and /health/ready endpoints.

Covers the AND-vs-OR semantics introduced in the reliability fixes:
- /health: 200 with `status: "degraded"` when one configured store is
  down but at least one is up. 503 only when every configured store is
  unreachable. The 200-with-degraded-body shape is required so the
  Docker HEALTHCHECK in Dockerfile.backend doesn't restart healthy
  containers on transient log-store hiccups.
- /health/ready: strict AND. 503 when any configured store is
  unreachable.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from routing.routers import RouteConfig

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def _set_store_health(store, healthy: bool) -> None:
    """Configure the mock store's health_check to return ``healthy``.

    The shared mock fixtures install AsyncMock for every method but do
    not pin a return value for health_check (defaults to MagicMock,
    which is truthy). Pin it explicitly here so each test owns its
    state.
    """
    store.health_check = AsyncMock(return_value=healthy)


async def test_health_both_stores_ok_returns_healthy(test_client, app_services) -> None:
    await _set_store_health(app_services.operational_store, True)
    await _set_store_health(app_services.log_store, True)

    resp = await test_client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["database_connected"] is True


async def test_health_op_only_up_returns_200_degraded(test_client, app_services) -> None:
    """log_store down, op_store up → 200 with degraded body.

    Must NOT return 503 — that would trigger Docker HEALTHCHECK restart
    loops via Dockerfile.backend when Postgres has transient hiccups.
    """
    await _set_store_health(app_services.operational_store, True)
    await _set_store_health(app_services.log_store, False)

    resp = await test_client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["database_connected"] is True
    assert body["stores"]["operational_store"]["status"] == "ok"
    assert body["stores"]["log_store"]["status"] == "error"


async def test_health_log_only_up_returns_200_degraded(test_client, app_services) -> None:
    """op_store down, log_store up → 200 with degraded body."""
    await _set_store_health(app_services.operational_store, False)
    await _set_store_health(app_services.log_store, True)

    resp = await test_client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["stores"]["operational_store"]["status"] == "error"
    assert body["stores"]["log_store"]["status"] == "ok"


async def test_health_both_stores_down_returns_503(test_client, app_services) -> None:
    await _set_store_health(app_services.operational_store, False)
    await _set_store_health(app_services.log_store, False)

    resp = await test_client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["reason"] == "database_disconnected"


async def test_health_ready_both_stores_ok_returns_200(test_client, app_services) -> None:
    await _set_store_health(app_services.operational_store, True)
    await _set_store_health(app_services.log_store, True)

    resp = await test_client.get("/health/ready")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"


async def test_health_ready_op_only_up_returns_503(test_client, app_services) -> None:
    """Strict AND: log_store down → not ready, even though /health is 200."""
    await _set_store_health(app_services.operational_store, True)
    await _set_store_health(app_services.log_store, False)

    resp = await test_client.get("/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["reason"] == "store_degraded"


async def test_health_ready_log_only_up_returns_503(test_client, app_services) -> None:
    await _set_store_health(app_services.operational_store, False)
    await _set_store_health(app_services.log_store, True)

    resp = await test_client.get("/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"


async def test_health_ready_both_down_returns_503(test_client, app_services) -> None:
    await _set_store_health(app_services.operational_store, False)
    await _set_store_health(app_services.log_store, False)

    resp = await test_client.get("/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"


async def test_discovery_and_health_counts_hide_unpublished_routes(
    test_client,
    app_services,
) -> None:
    await _set_store_health(app_services.operational_store, True)
    await _set_store_health(app_services.log_store, True)
    baseline_count = sum(
        getattr(route, "published", True) for route in app_services.router.routes.values()
    )
    staged_adapter = SimpleNamespace(
        config=SimpleNamespace(
            id="staged-model",
            provider="staged-provider",
            base_url="https://staged.invalid/v1",
        )
    )
    app_services.router.routes["staged-model"] = RouteConfig(
        adapters=[(staged_adapter, 1.0)],
        canonical_model_id="staged-model",
        published=False,
    )

    health_response = await test_client.get("/health")
    deep_response = await test_client.get("/health/deep")
    routing_response = await test_client.get("/routing")

    assert health_response.json()["routes_configured"] == baseline_count
    assert deep_response.json()["routes_configured"] == baseline_count
    assert "staged-model" not in routing_response.json()["routes"]


async def test_health_reports_no_database_when_none_is_configured(
    test_client, app_services, monkeypatch
):
    """A DB-less deployment must not claim a connected database.

    Regression: the endpoint derived `database_connected` from the store
    health flag, which reports an unconfigured database as healthy — so the
    "no database configured" branch was unreachable and the response said
    database_configured/connected were both true with no database at all.
    """
    monkeypatch.setenv("DB_ENABLED", "false")
    app_services.operational_store = None
    app_services.log_store = None

    resp = await test_client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["database_configured"] is False
    assert body["database_connected"] is False


async def test_health_does_not_call_a_failed_database_unconfigured(
    test_client, app_services, monkeypatch
):
    """Wanting a database and not having one is an outage, not a shape.

    Both states leave the stores as None, and `_test_store_health` cannot tell
    them apart. Reporting the second as healthy is how a gateway whose database
    never came up keeps taking traffic from a load balancer that believes it.
    """
    monkeypatch.setenv("DB_ENABLED", "true")
    app_services.operational_store = None
    app_services.log_store = None

    resp = await test_client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["reason"] == "database_unavailable_at_startup"
    assert body["database_configured"] is True
    assert body["database_connected"] is False
