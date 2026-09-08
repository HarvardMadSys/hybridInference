from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.deps import AppServices
from serving.servers.middleware.error import (
    FallbackErrorMiddleware,
    install_error_handlers,
)
from serving.servers.routers import admin, models
from serving.utils.jwt import create_access_token


class _EchoAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ):  # pragma: no cover
        yield self.format_stream_chunk(model=self.config.id, content="ok")


def _cfg(model_id: str) -> ModelConfig:
    return ModelConfig(
        id=model_id,
        name=model_id,
        provider="test",
        base_url="http://test",
    )


@pytest.mark.asyncio
async def test_admin_routing_status_with_and_without_manager(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-routing-token")
    headers = {"Authorization": "Bearer test-admin-routing-token"}
    # Build router with one model
    router = RouteExecutor()
    router.register_route("m1", [(_EchoAdapter(_cfg("m1")), 1.0)])

    # Case 1: with routing manager
    class _Mgr:
        def get_status(self) -> dict[str, Any]:
            return {
                "loaded": True,
                "strategy": "fixed",
                "deployments": {"local": 1, "remote": 0},
            }

    app = FastAPI()
    app.state.services = AppServices(router=router, db_logger=None, routing_manager=_Mgr())  # type: ignore[attr-defined]
    app.include_router(models.router)
    app.include_router(admin.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/admin/routing", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "routes" in data and "manager_status" in data
        assert "m1" in data["routes"]

    # Case 2: without routing manager
    app2 = FastAPI()
    app2.state.services = AppServices(router=router, db_logger=None, routing_manager=None)  # type: ignore[attr-defined]
    app2.include_router(models.router)
    app2.include_router(admin.router)

    transport2 = ASGITransport(app=app2)
    async with AsyncClient(transport=transport2, base_url="http://test") as client:
        resp = await client.get("/admin/routing", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "routes" in data and "manager_status" not in data


@pytest.fixture
def admin_routing_app(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-admin-routing-secret-key-at-least-32-bytes")
    router = RouteExecutor()
    router.register_route("m1", [(_EchoAdapter(_cfg("m1")), 1.0)])
    manager = Mock()
    manager.get_status.return_value = {"loaded": True, "strategy": "fixed"}
    app = FastAPI()
    app.state.services = AppServices(router=router, routing_manager=manager)
    app.include_router(admin.router)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("user_auth_enabled", ["true", "false"])
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic invalid"}, {"Authorization": "Bearer invalid"}],
    ids=["anonymous", "wrong-scheme", "invalid-token"],
)
async def test_admin_routing_requires_authentication(
    admin_routing_app, monkeypatch, user_auth_enabled, headers
):
    # Regression: the routing endpoint previously disclosed all routes without auth.
    monkeypatch.setenv("USER_AUTH_ENABLED", user_auth_enabled)
    async with AsyncClient(
        transport=ASGITransport(app=admin_routing_app), base_url="http://test"
    ) as client:
        resp = await client.get("/admin/routing", headers=headers)

    assert resp.status_code == 401
    assert "routes" not in resp.json()
    assert "manager_status" not in resp.json()
    admin_routing_app.state.services.routing_manager.get_status.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["free", "pro", "internal", "admin"])
async def test_admin_routing_requires_admin_role(admin_routing_app, role):
    op_store = AsyncMock()
    op_store.get_user_by_id.return_value = {
        "email": "user@example.test",
        "status": "active",
        "email_verified": True,
        "role": role,
    }
    admin_routing_app.state.services.operational_store = op_store
    token, _ = create_access_token(
        "user-1", "user@example.test", role=role, is_admin=role == "admin"
    )
    async with AsyncClient(
        transport=ASGITransport(app=admin_routing_app), base_url="http://test"
    ) as client:
        resp = await client.get("/admin/routing", headers={"Authorization": f"Bearer {token}"})

    op_store.get_user_by_id.assert_awaited_once_with("user-1")
    if role == "admin":
        assert resp.status_code == 200
        assert resp.json()["routes"]["m1"] == [
            {"provider": "test", "base_url": "http://test", "weight": "100%"}
        ]
        assert resp.json()["manager_status"] == {"loaded": True, "strategy": "fixed"}
        admin_routing_app.state.services.routing_manager.get_status.assert_called_once()
    else:
        assert resp.status_code == 403
        assert resp.json() == {"detail": "Admin access required."}
        admin_routing_app.state.services.routing_manager.get_status.assert_not_called()


@pytest.mark.asyncio
async def test_admin_routing_jwt_fails_closed_without_database(admin_routing_app):
    token, _ = create_access_token("admin-1", "admin@example.test", role="admin", is_admin=True)
    async with AsyncClient(
        transport=ASGITransport(app=admin_routing_app), base_url="http://test"
    ) as client:
        resp = await client.get("/admin/routing", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 503
    assert "routes" not in resp.json()
    assert "manager_status" not in resp.json()
    admin_routing_app.state.services.routing_manager.get_status.assert_not_called()


@pytest.mark.asyncio
async def test_error_middleware_integration_http_exception_and_generic():
    app = FastAPI()

    @app.get("/raise-http")
    def raise_http():
        # Will be wrapped into error payload with matching status code
        raise HTTPException(status_code=418, detail="teapot")

    @app.get("/raise-any")
    def raise_any():
        raise RuntimeError("boom")

    install_error_handlers(app)
    app.add_middleware(FallbackErrorMiddleware)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r1 = await client.get("/raise-http")
        assert r1.status_code == 418
        body1 = r1.json()
        assert body1["error"]["message"].lower().find("teapot") >= 0
        assert body1["error"]["code"] == 418

        r2 = await client.get("/raise-any")
        assert r2.status_code == 500
        body2 = r2.json()
        assert body2["error"]["message"].lower().find("internal server error") >= 0
        assert body2["error"]["code"] == 500
