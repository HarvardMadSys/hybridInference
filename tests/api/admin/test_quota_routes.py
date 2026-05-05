"""API surface tests for /admin/quota/role-apply{,-preview}."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_admin():
    """Build a minimal FastAPI app mounting the admin quota router with mocked deps."""
    from fastapi import FastAPI

    from serving.servers.routers.admin import quota as quota_router

    app = FastAPI()
    app.include_router(quota_router.router)

    op_store = AsyncMock()
    op_store.count_active_keys_for_role.return_value = (5, 4)
    op_store.apply_role_quota.return_value = 5

    rt = AsyncMock()
    rt.get_float.return_value = 250.0

    from serving.config.runtime_settings import get_runtime_settings
    from serving.servers.deps import get_operational_store, verify_admin_access

    app.dependency_overrides[get_operational_store] = lambda: op_store
    app.dependency_overrides[verify_admin_access] = lambda: "admin-id"
    app.dependency_overrides[get_runtime_settings] = lambda: rt
    return app, op_store, rt


def test_preview_returns_count_and_quota(app_with_admin):
    app, op_store, rt = app_with_admin
    client = TestClient(app)
    resp = client.get("/admin/quota/role-apply-preview", params={"role": "pro"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "pro"
    assert float(body["quota"]) == 250.0
    assert body["keys_affected"] == 5
    assert body["users_affected"] == 4
    rt.get_float.assert_awaited_with("user_daily_quota_pro")
    op_store.count_active_keys_for_role.assert_awaited_with("pro")


def test_apply_returns_keys_updated(app_with_admin):
    app, op_store, _rt = app_with_admin
    client = TestClient(app)
    resp = client.post("/admin/quota/role-apply", json={"role": "pro"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "pro"
    assert float(body["quota"]) == 250.0
    assert body["keys_updated"] == 5
    op_store.apply_role_quota.assert_awaited_with("pro", Decimal("250.0"))


def test_invalid_role_rejected(app_with_admin):
    app, _, _ = app_with_admin
    client = TestClient(app)
    assert (
        client.get("/admin/quota/role-apply-preview", params={"role": "ghost"}).status_code == 422
    )
    assert client.post("/admin/quota/role-apply", json={"role": "ghost"}).status_code == 422
