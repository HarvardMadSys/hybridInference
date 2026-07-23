"""Dependency-free characterization of the browser auth/session boundary."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import auth_routes, playground, user_routes


@pytest.fixture
async def auth_session_contract(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("JWT_SECRET_KEY", "contract-only-secret-that-is-long-enough")
    monkeypatch.setattr(auth_routes.password_utils, "verify_password", lambda *_args: True)
    monkeypatch.setattr(
        auth_routes,
        "check_and_record_login",
        AsyncMock(return_value=(True, None)),
    )
    monkeypatch.setattr(auth_routes, "is_admin_email", lambda _email: False)
    monkeypatch.setattr(auth_routes.settings, "signup_require_email_verification", False)
    # The browser contract describes the production-safe default. CI explicitly
    # disables Secure cookies for its HTTP test environment, so isolate this
    # characterization from that runner-level override.
    monkeypatch.setattr(auth_routes.settings, "cookie_secure", True)

    def _no_runtime_settings():
        raise RuntimeError("runtime settings intentionally absent in contract fixture")

    monkeypatch.setattr(
        "serving.config.runtime_settings.get_runtime_settings_instance",
        _no_runtime_settings,
    )

    now = datetime.now(timezone.utc)
    user = {
        "id": "contract-session-user",
        "email": "session@example.com",
        "password_hash": "not-used",
        "user_name": "Contract Session User",
        "role": "free",
        "status": "active",
        "email_verified": True,
        "created_at": now,
        "last_login_at": None,
    }
    store = AsyncMock()
    store.get_user_by_email.return_value = user
    store.get_user_by_id.return_value = user

    app = FastAPI()
    install_error_handlers(app)
    app.state.services = AppServices(
        router=RouteExecutor(),
        operational_store=store,
    )
    app.include_router(auth_routes.router)
    app.include_router(user_routes.router)
    app.include_router(playground.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://contract.test") as client:
        yield client, store, user


@pytest.mark.asyncio
async def test_login_refresh_cookie_and_me_contract(auth_session_contract) -> None:
    client, store, user = auth_session_contract
    login = await client.post(
        "/auth/login",
        json={"email": user["email"], "password": "contract-password"},
    )

    assert login.status_code == 200, login.text
    assert login.json()["token_type"] == "bearer"
    assert login.json()["user"]["status"] == "active"
    original_refresh = login.cookies.get("refresh_token")
    assert original_refresh

    cookie_header = login.headers["set-cookie"].lower()
    assert "refresh_token=" in cookie_header
    assert "httponly" in cookie_header
    assert "secure" in cookie_header
    assert "samesite=lax" in cookie_header
    assert "path=/" in cookie_header

    store.get_session_by_token_hash.return_value = {
        "id": "stored-session",
        "user_id": user["id"],
        "sid": "contract-browser-session",
        "revoked": False,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
    }
    refreshed = await client.post("/auth/refresh")

    assert refreshed.status_code == 200
    assert refreshed.json()["token_type"] == "bearer"
    rotated_refresh = refreshed.cookies.get("refresh_token")
    assert rotated_refresh
    assert rotated_refresh != original_refresh
    store.rotate_session.assert_awaited_once()

    me = await client.get(
        "/user/me",
        headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json()["id"] == user["id"]
    assert me.json()["role"] == "free"

    unauthenticated = await client.get("/user/me")
    assert unauthenticated.status_code == 401


@pytest.mark.asyncio
async def test_status_and_permission_are_computed_from_current_backend_state(
    auth_session_contract,
) -> None:
    client, _store, user = auth_session_contract
    login = await client.post(
        "/auth/login",
        json={"email": user["email"], "password": "contract-password"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    denied = await client.get("/control/v1/playground/models", headers=headers)
    assert denied.status_code == 403

    # The JWT still says "free"; current DB state is the permission truth.
    user["role"] = "internal"
    allowed = await client.get("/control/v1/playground/models", headers=headers)
    assert allowed.status_code == 200
    assert allowed.json() == {"models": []}

    user["status"] = "suspended"
    inactive = await client.get("/user/me", headers=headers)
    assert inactive.status_code == 403
