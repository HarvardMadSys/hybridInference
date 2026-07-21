"""Tests for admin-only auth verification and playground endpoints."""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

# Add project root to Python path for direct test execution.
project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from routing.executor import RouteExecutor
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.config import settings as settings_module
from serving.servers import deps as deps_module
from serving.servers.deps import AppServices
from serving.servers.routers import auth_routes, internal, playground, user_routes
from serving.stream import done_sentinel, make_final_usage_chunk
from serving.utils.jwt import generate_ulid
from serving.utils.password import hash_password

pytest_plugins = ["tests.servers.conftest_auth"]


class _PlaygroundAdapter(BaseAdapter):
    """Simple streaming adapter for playground tests."""

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        yield self.format_stream_chunk(model=self.config.id, content="hello ")
        yield (
            'data: {"id":"chunk-1","object":"chat.completion.chunk","created":123,'
            f'"model":"{self.config.id}","choices":[{{"index":0,"delta":{{"content":"world"}},'
            '"finish_reason":null}],"_routing":{"provider":"test","base_url":"http://secret"}}\n\n'
        )
        yield make_final_usage_chunk(
            model=self.config.id,
            messages=messages,
            total_content="hello world",
            provider="test",
            base_url="http://secret",
        )
        yield done_sentinel()


def _cfg(model_id: str) -> ModelConfig:
    return ModelConfig(
        id=model_id,
        name=model_id,
        provider="test",
        base_url="http://test",
    )


@pytest.mark.asyncio
async def test_playground_models_hide_unpublished_routes() -> None:
    router = RouteExecutor()
    visible = _PlaygroundAdapter(_cfg("visible-model"))
    staged = _PlaygroundAdapter(_cfg("staged-model"))
    router.register_route("visible-model", [(visible, 1.0)])
    router.register_route("staged-model", [(staged, 1.0)], published=False)

    result = await playground.list_models(_admin={}, router_exec=router)

    assert [model["id"] for model in result["models"]] == ["visible-model"]


def _create_test_user(**overrides: Any) -> dict[str, Any]:
    """Create a user payload suitable for inserting into the test database."""
    user_id = generate_ulid()
    password = overrides.get("password", "SecurePass123!")
    defaults = {
        "id": user_id,
        "email": f"test_{user_id[:8]}@example.com",
        "password": password,
        "password_hash": hash_password(password),
        "user_name": f"Test User {user_id[:8]}",
        "status": "active",
        "email_verified": True,
    }
    result = dict(defaults)
    result.update(overrides)
    if "password" in overrides and "password_hash" not in overrides:
        result["password_hash"] = hash_password(overrides["password"])
    return result


async def _insert_user(op_store, **overrides: Any) -> dict[str, Any]:
    """Insert a user row via the operational store."""
    user = _create_test_user(**overrides)
    await op_store.create_user(
        user_id=user["id"],
        email=user["email"],
        password_hash=user["password_hash"],
        user_name=user["user_name"],
        email_verified=user["email_verified"],
        status=user["status"],
    )
    return user


async def _login(client: AsyncClient, email: str, password: str) -> tuple[str, str]:
    """Login and return access token plus refresh token cookie."""
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    access_token = response.json()["access_token"]
    refresh_token = response.cookies.get("refresh_token")
    assert refresh_token is not None
    return access_token, refresh_token


@pytest_asyncio.fixture
async def admin_mode_app(auth_backend):
    """App with auth, internal, and playground routers."""
    operational_store, log_store, db_logger, _ = auth_backend

    router = RouteExecutor()
    router.register_route("playground-model", [(_PlaygroundAdapter(_cfg("playground-model")), 1.0)])

    services = AppServices(
        router=router,
        db_logger=db_logger,
        operational_store=operational_store,
        log_store=log_store,
        routing_manager=None,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = services
        yield

    app = FastAPI(title="Admin Mode Test App", lifespan=lifespan)
    app.state.services = services  # type: ignore[attr-defined]
    app.include_router(auth_routes.router)
    app.include_router(user_routes.router)
    app.include_router(internal.router)
    app.include_router(playground.router)
    return app


@pytest_asyncio.fixture
async def admin_mode_client(admin_mode_app: FastAPI):
    """HTTP client for admin mode integration tests."""
    transport = ASGITransport(app=admin_mode_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestAdminModeUnit:
    """Unit tests that do not require a real database."""

    @pytest.mark.asyncio
    async def test_require_admin_allows_admin_role(self):
        current_user = {"email": "admin@example.com", "user_id": "u1", "role": "admin"}

        result = await deps_module.require_admin(current_user=current_user)

        assert result is current_user

    @pytest.mark.asyncio
    async def test_require_admin_rejects_non_admin(self):
        with pytest.raises(HTTPException) as exc:
            await deps_module.require_admin(
                current_user={"email": "user@example.com", "user_id": "u1", "role": "free"}
            )

        assert exc.value.status_code == 403

    def test_sanitize_chunk_strips_routing_metadata(self):
        chunk = (
            'data: {"id":"chunk-1","object":"chat.completion.chunk","created":123,'
            '"model":"playground-model","choices":[{"index":0,"delta":{"content":"hello"},'
            '"finish_reason":null}],"_routing":{"provider":"test"}}\n\n'
        )

        sanitized = playground._sanitize_chunk(chunk)

        assert "_routing" not in sanitized
        parsed = json.loads(sanitized[6:])
        assert parsed["choices"][0]["delta"]["content"] == "hello"


@pytest.mark.dbtest
class TestPlaygroundAccess:
    """Tests for admin-only playground endpoints."""

    @pytest.mark.asyncio
    async def test_playground_models_returns_403_for_non_admin(
        self,
        admin_mode_client: AsyncClient,
        auth_backend,
        monkeypatch,
        clean_auth_tables,
    ):
        operational_store, _, _, _ = auth_backend
        monkeypatch.setattr(settings_module.settings, "admin_emails", "")
        user = await _insert_user(operational_store)
        access_token, _ = await _login(admin_mode_client, user["email"], user["password"])

        response = await admin_mode_client.get(
            "/internal/playground/models",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_playground_models_returns_model_list_for_admin(
        self,
        admin_mode_client: AsyncClient,
        auth_backend,
        monkeypatch,
        clean_auth_tables,
    ):
        operational_store, _, _, _ = auth_backend
        user = await _insert_user(operational_store)
        monkeypatch.setattr(settings_module.settings, "admin_emails", user["email"].lower())
        access_token, _ = await _login(admin_mode_client, user["email"], user["password"])

        response = await admin_mode_client.get(
            "/internal/playground/models",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["models"]) == 1
        model = data["models"][0]
        assert model["id"] == "playground-model"
        assert model["name"] == "playground-model"
        assert model["provider"] == "test"
        assert model["providers"] == [{"id": "test", "name": "test"}]

    @pytest.mark.asyncio
    async def test_playground_models_uses_db_role_for_admin_check(
        self,
        admin_mode_client: AsyncClient,
        auth_backend,
        monkeypatch,
        clean_auth_tables,
    ):
        operational_store, _, _, _ = auth_backend
        user = await _insert_user(operational_store)
        monkeypatch.setattr(settings_module.settings, "admin_emails", user["email"].lower())
        access_token, _ = await _login(admin_mode_client, user["email"], user["password"])

        # Change email after login — JWT still has old email, but DB role check uses user_id
        await operational_store.update_user_fields(user["id"], email="revoked-admin@example.com")

        response = await admin_mode_client.get(
            "/internal/playground/models",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_playground_chat_strips_internal_routing_metadata(
        self,
        admin_mode_client: AsyncClient,
        auth_backend,
        monkeypatch,
        clean_auth_tables,
    ):
        operational_store, _, _, _ = auth_backend
        user = await _insert_user(operational_store)
        monkeypatch.setattr(settings_module.settings, "admin_emails", user["email"].lower())
        access_token, _ = await _login(admin_mode_client, user["email"], user["password"])

        async with admin_mode_client.stream(
            "POST",
            "/internal/playground/chat",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "model": "playground-model",
                "messages": [{"role": "user", "content": "hi"}],
                "provider": "test",
            },
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-cache, no-transform"
            assert response.headers["x-accel-buffering"] == "no"

            lines: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    lines.append(line)

        assert any(line == "data: [DONE]" for line in lines)
        assert all("_routing" not in line for line in lines)

        content = "".join(
            json.loads(line[6:])["choices"][0]["delta"].get("content", "")
            for line in lines
            if line != "data: [DONE]"
        )
        assert content == "hello world"
