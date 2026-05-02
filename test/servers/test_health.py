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

from unittest.mock import AsyncMock

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def _set_store_health(store, healthy: bool) -> None:
    """Configure the mock store's health_check to return ``healthy``.

    The shared mock fixtures install AsyncMock for every method but do
    not pin a return value for health_check (defaults to MagicMock,
    which is truthy). Pin it explicitly here so each test owns its
    state.
    """
    store.health_check = AsyncMock(return_value=healthy)


async def test_health_both_stores_ok_returns_healthy(
    test_client, app_services
) -> None:
    await _set_store_health(app_services.operational_store, True)
    await _set_store_health(app_services.log_store, True)

    resp = await test_client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["database_connected"] is True


async def test_health_op_only_up_returns_200_degraded(
    test_client, app_services
) -> None:
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


async def test_health_log_only_up_returns_200_degraded(
    test_client, app_services
) -> None:
    """op_store down, log_store up → 200 with degraded body."""
    await _set_store_health(app_services.operational_store, False)
    await _set_store_health(app_services.log_store, True)

    resp = await test_client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["stores"]["operational_store"]["status"] == "error"
    assert body["stores"]["log_store"]["status"] == "ok"


async def test_health_both_stores_down_returns_503(
    test_client, app_services
) -> None:
    await _set_store_health(app_services.operational_store, False)
    await _set_store_health(app_services.log_store, False)

    resp = await test_client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["reason"] == "database_disconnected"


async def test_health_ready_both_stores_ok_returns_200(
    test_client, app_services
) -> None:
    await _set_store_health(app_services.operational_store, True)
    await _set_store_health(app_services.log_store, True)

    resp = await test_client.get("/health/ready")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"


async def test_health_ready_op_only_up_returns_503(
    test_client, app_services
) -> None:
    """Strict AND: log_store down → not ready, even though /health is 200."""
    await _set_store_health(app_services.operational_store, True)
    await _set_store_health(app_services.log_store, False)

    resp = await test_client.get("/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["reason"] == "store_degraded"


async def test_health_ready_log_only_up_returns_503(
    test_client, app_services
) -> None:
    await _set_store_health(app_services.operational_store, False)
    await _set_store_health(app_services.log_store, True)

    resp = await test_client.get("/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"


async def test_health_ready_both_down_returns_503(
    test_client, app_services
) -> None:
    await _set_store_health(app_services.operational_store, False)
    await _set_store_health(app_services.log_store, False)

    resp = await test_client.get("/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
