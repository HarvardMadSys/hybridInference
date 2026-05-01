"""Tests for admin-only Grafana gating and playground endpoints."""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

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
from serving.config.settings import get_settings
from serving.servers import deps as deps_module
from serving.servers.deps import AppServices
from serving.servers.routers import auth_routes, internal, playground, user_routes
from serving.storage.database import DatabaseLogger
from serving.stream import done_sentinel, make_final_usage_chunk
from serving.utils.jwt import generate_ulid
from serving.utils.password import hash_password


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


class _AcquireContext:
    """Async context manager for mocked database connections."""

    def __init__(self, connection: AsyncMock) -> None:
        self._connection = connection

    async def __aenter__(self) -> AsyncMock:
        return self._connection

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


def _cfg(model_id: str) -> ModelConfig:
    return ModelConfig(
        id=model_id,
        name=model_id,
        provider="test",
        base_url="http://test",
    )


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


async def _insert_user(auth_db_logger, **overrides: Any) -> dict[str, Any]:
    """Insert a user row into the auth test database."""
    user = _create_test_user(**overrides)
    async with auth_db_logger.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (id, email, password_hash, user_name, status, email_verified)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            user["id"],
            user["email"].lower(),
            user["password_hash"],
            user["user_name"],
            user["status"],
            user["email_verified"],
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


def _mock_db_logger_with_rows(*rows: Any):
    """Create a db logger whose fetchrow returns the provided rows in order."""
    connection = AsyncMock()
    connection.fetchrow = AsyncMock(side_effect=list(rows))
    pool = MagicMock()
    pool.acquire.return_value = _AcquireContext(connection)
    logger = MagicMock()
    logger.pool = pool
    return logger, connection


@pytest.fixture
def auth_env(monkeypatch):
    """Set auth-related environment variables for integration tests."""
    test_env = {
        "DB_ENABLED": "true",
        "DB_HOST": os.getenv("TEST_DB_HOST", "localhost"),
        "DB_PORT": os.getenv("TEST_DB_PORT", "5432"),
        "DB_NAME": os.getenv("TEST_DB_NAME", "freeinference_test_db"),
        "DB_USER": os.getenv("TEST_DB_USER", "postgres"),
        "DB_PASSWORD": os.getenv("TEST_DB_PASSWORD", "postgres"),
        "JWT_SECRET_KEY": "test-secret-key-for-testing-only-do-not-use-in-production",
        "JWT_ALGORITHM": "HS256",
        "JWT_ACCESS_TOKEN_EXPIRE_MINUTES": "15",
        "JWT_REFRESH_TOKEN_EXPIRE_DAYS": "30",
        "API_KEY_SECRET": "test-api-key-secret-for-testing-only",
        "COOKIE_SECURE": "0",
        "COOKIE_SAMESITE": "lax",
        "SIGNUP_ENABLED": "1",
        "SIGNUP_DEFAULT_TIER": "free",
        "SIGNUP_DEFAULT_DAILY_QUOTA_USD": "100.00",
        "SIGNUP_REQUIRE_EMAIL_VERIFICATION": "0",
        "BASE_URL": "http://localhost:8000",
    }
    for key, value in test_env.items():
        monkeypatch.setenv(key, value)

    get_settings.cache_clear()
    return test_env


@pytest_asyncio.fixture
async def auth_db_logger(auth_env):
    """Real database logger for auth/session integration tests."""
    db_config = {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": os.getenv("TEST_DB_NAME", "freeinference_test_db"),
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }

    logger = DatabaseLogger(db_config=db_config)
    try:
        await logger.initialize()
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")

    yield logger
    await logger.cleanup()


@pytest_asyncio.fixture
async def clean_auth_tables(auth_db_logger):
    """Clean auth-related tables before and after each test."""
    async with auth_db_logger.pool.acquire() as conn:
        await conn.execute("DELETE FROM email_verification_tokens")
        await conn.execute("DELETE FROM password_reset_tokens")
        await conn.execute("DELETE FROM auth_sessions")
        await conn.execute("DELETE FROM api_keys WHERE account_id IS NOT NULL")
        await conn.execute("DELETE FROM users")

    yield

    async with auth_db_logger.pool.acquire() as conn:
        await conn.execute("DELETE FROM email_verification_tokens")
        await conn.execute("DELETE FROM password_reset_tokens")
        await conn.execute("DELETE FROM auth_sessions")
        await conn.execute("DELETE FROM api_keys WHERE account_id IS NOT NULL")
        await conn.execute("DELETE FROM users")


@pytest_asyncio.fixture
async def admin_mode_app(auth_db_logger):
    """App with auth, internal, and playground routers."""
    router = RouteExecutor()
    router.register_route("playground-model", [(_PlaygroundAdapter(_cfg("playground-model")), 1.0)])

    services = AppServices(
        router=router,
        db_logger=auth_db_logger,
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

    @pytest.mark.asyncio
    async def test_verify_grafana_rejects_revoked_session(self, monkeypatch):
        monkeypatch.setattr(settings_module.settings, "admin_emails", "admin@example.com")
        db_logger, _ = _mock_db_logger_with_rows(
            {"user_id": "u1", "expires_at": "9999-01-01T00:00:00+00:00", "revoked": True}
        )

        with pytest.raises(HTTPException) as exc:
            await internal.verify_grafana(refresh_token="token", db_logger=db_logger)

        assert exc.value.status_code == 401

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


class TestGrafanaVerification:
    """Tests for the internal Grafana auth endpoint."""

    @pytest.mark.asyncio
    async def test_verify_grafana_returns_401_without_cookie(self, admin_mode_client: AsyncClient):
        response = await admin_mode_client.get("/internal/verify-grafana")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_verify_grafana_returns_403_for_non_admin(
        self,
        admin_mode_client: AsyncClient,
        auth_db_logger,
        monkeypatch,
        clean_auth_tables,
    ):
        monkeypatch.setattr(settings_module.settings, "admin_emails", "")
        user = await _insert_user(auth_db_logger)
        _, refresh_token = await _login(admin_mode_client, user["email"], user["password"])

        response = await admin_mode_client.get(
            "/internal/verify-grafana",
            cookies={"refresh_token": refresh_token},
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_verify_grafana_returns_200_for_admin(
        self,
        admin_mode_client: AsyncClient,
        auth_db_logger,
        monkeypatch,
        clean_auth_tables,
    ):
        user = await _insert_user(auth_db_logger)
        monkeypatch.setattr(settings_module.settings, "admin_emails", user["email"].lower())
        _, refresh_token = await _login(admin_mode_client, user["email"], user["password"])

        response = await admin_mode_client.get(
            "/internal/verify-grafana",
            cookies={"refresh_token": refresh_token},
        )

        assert response.status_code == 200


class TestPlaygroundAccess:
    """Tests for admin-only playground endpoints."""

    @pytest.mark.asyncio
    async def test_playground_models_returns_403_for_non_admin(
        self,
        admin_mode_client: AsyncClient,
        auth_db_logger,
        monkeypatch,
        clean_auth_tables,
    ):
        monkeypatch.setattr(settings_module.settings, "admin_emails", "")
        user = await _insert_user(auth_db_logger)
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
        auth_db_logger,
        monkeypatch,
        clean_auth_tables,
    ):
        user = await _insert_user(auth_db_logger)
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
        auth_db_logger,
        monkeypatch,
        clean_auth_tables,
    ):
        user = await _insert_user(auth_db_logger)
        monkeypatch.setattr(settings_module.settings, "admin_emails", user["email"].lower())
        access_token, _ = await _login(admin_mode_client, user["email"], user["password"])

        async with auth_db_logger.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET email = $1 WHERE id = $2",
                "revoked-admin@example.com",
                user["id"],
            )

        response = await admin_mode_client.get(
            "/internal/playground/models",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_playground_chat_strips_internal_routing_metadata(
        self,
        admin_mode_client: AsyncClient,
        auth_db_logger,
        monkeypatch,
        clean_auth_tables,
    ):
        user = await _insert_user(auth_db_logger)
        monkeypatch.setattr(settings_module.settings, "admin_emails", user["email"].lower())
        access_token, _ = await _login(admin_mode_client, user["email"], user["password"])

        async with admin_mode_client.stream(
            "POST",
            "/internal/playground/chat",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "model": "playground-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-cache"
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
