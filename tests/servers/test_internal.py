"""Integration tests for internal auth_request endpoints.

Run with: make test-db
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.routers import internal
from serving.servers.routers.auth_routes import hash_refresh_token
from tests.fixtures.auth_factories import create_test_user

pytest_plugins = ["tests.servers.conftest_auth"]

pytestmark = pytest.mark.dbtest


async def _set_user_role(op_store, user_id: str, role: str) -> None:
    """Update the user's role for a test scenario."""
    await op_store.update_user_fields(user_id, role=role)


async def _create_refresh_session(op_store, user_id: str, refresh_token: str) -> None:
    """Insert a valid refresh session for the given user."""
    await op_store.create_session(
        session_id=str(uuid4()),
        user_id=user_id,
        refresh_token_hash=hash_refresh_token(refresh_token),
        jti=str(uuid4()),
        sid=str(uuid4()),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )


@pytest_asyncio.fixture
async def auth_test_user(auth_backend, clean_auth_tables):
    """Create a user backed by the auth-specific DB fixtures."""
    operational_store, _, _, _ = auth_backend
    user_data = create_test_user()

    await operational_store.create_user(
        user_id=user_data["id"],
        email=user_data["email"],
        password_hash=user_data["password_hash"],
        user_name=user_data["user_name"],
        email_verified=user_data["email_verified"],
        status=user_data["status"],
    )

    yield user_data


@pytest_asyncio.fixture
async def internal_test_app(auth_app_services):
    """Create a FastAPI app with internal routes registered."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = auth_app_services
        yield

    app = FastAPI(title="Internal Test API", version="1.0.0", lifespan=lifespan)
    app.state.services = auth_app_services
    app.include_router(internal.router)
    return app


@pytest_asyncio.fixture
async def internal_client(internal_test_app):
    """Create an async client for internal routes."""
    transport = ASGITransport(app=internal_test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestVerifyAdmin:
    """Test the admin-only auth_request endpoint."""

    @pytest.mark.asyncio
    async def test_verify_admin_allows_admin_session(
        self,
        internal_client: AsyncClient,
        auth_backend,
        auth_test_user,
    ) -> None:
        """Admin sessions should pass the auth_request check."""
        operational_store, _, _, _ = auth_backend
        refresh_token = "test-refresh-admin"
        await _set_user_role(operational_store, auth_test_user["id"], "admin")
        await _create_refresh_session(operational_store, auth_test_user["id"], refresh_token)

        response = await internal_client.get(
            "/internal/verify-admin",
            cookies={"refresh_token": refresh_token},
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_verify_admin_rejects_internal_session(
        self,
        internal_client: AsyncClient,
        auth_backend,
        auth_test_user,
    ) -> None:
        """Internal sessions should not pass the admin auth_request check."""
        operational_store, _, _, _ = auth_backend
        refresh_token = "test-refresh-internal"
        await _set_user_role(operational_store, auth_test_user["id"], "internal")
        await _create_refresh_session(operational_store, auth_test_user["id"], refresh_token)

        response = await internal_client.get(
            "/internal/verify-admin",
            cookies={"refresh_token": refresh_token},
        )

        assert response.status_code == 403
        assert response.json()["detail"] == "Admin access required."

    @pytest.mark.asyncio
    async def test_verify_admin_rejects_invalid_session(
        self,
        internal_client: AsyncClient,
        auth_backend,
    ) -> None:
        """Unknown refresh tokens should be rejected."""
        response = await internal_client.get(
            "/internal/verify-admin",
            cookies={"refresh_token": "missing-session"},
        )

        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid session."
