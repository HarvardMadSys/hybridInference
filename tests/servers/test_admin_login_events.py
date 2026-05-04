"""Tests for the DELETE /admin/login-events endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router


@pytest.fixture
def mock_op_store() -> MagicMock:
    op = MagicMock()
    op.purge_login_events_older_than = AsyncMock(return_value=42)
    op.purge_login_events_for_user = AsyncMock(return_value=7)
    op.log_admin_action = AsyncMock()
    return op


@pytest.fixture
async def admin_client(monkeypatch, mock_op_store):
    app = FastAPI()
    services = AppServices(
        router=MagicMock(),
        db_logger=MagicMock(),
        operational_store=mock_op_store,
        log_store=MagicMock(),
    )
    app.state.services = services
    app.include_router(admin_router.router)

    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")

    mock_log_action = AsyncMock()
    monkeypatch.setattr(
        "serving.servers.routers.admin.login_events.log_admin_action", mock_log_action
    )
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    try:
        yield client, mock_op_store, mock_log_action
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_purge_by_age(admin_client):
    client, op, log_action = admin_client
    resp = await client.delete(
        "/admin/login-events?older_than_days=7",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 42}
    op.purge_login_events_older_than.assert_awaited_once_with(7)
    op.purge_login_events_for_user.assert_not_awaited()
    log_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_purge_by_user(admin_client):
    client, op, log_action = admin_client
    resp = await client.delete(
        "/admin/login-events?user_id=u1",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 7}
    op.purge_login_events_for_user.assert_awaited_once_with("u1")
    op.purge_login_events_older_than.assert_not_awaited()
    log_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_purge_neither_param_returns_400(admin_client):
    client, op, _ = admin_client
    resp = await client.delete(
        "/admin/login-events",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert resp.status_code == 400
    op.purge_login_events_older_than.assert_not_awaited()
    op.purge_login_events_for_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_purge_both_params_returns_400(admin_client):
    client, op, _ = admin_client
    resp = await client.delete(
        "/admin/login-events?older_than_days=7&user_id=u1",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert resp.status_code == 400
    op.purge_login_events_older_than.assert_not_awaited()
    op.purge_login_events_for_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_purge_requires_admin_auth(admin_client):
    client, _, _ = admin_client
    resp = await client.delete("/admin/login-events?older_than_days=7")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_purge_older_than_days_must_be_positive(admin_client):
    client, _, _ = admin_client
    resp = await client.delete(
        "/admin/login-events?older_than_days=0",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert resp.status_code == 422  # FastAPI Query(ge=1) validation
