"""Integration tests for internal auth_request endpoints.

Requires a running PostgreSQL test database (see TEST_DB_* env vars).
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
from test.fixtures.auth_factories import create_test_user

pytest_plugins = ["test.servers.conftest_auth"]

pytestmark = pytest.mark.dbtest


async def _set_user_role(auth_db_logger, user_id: str, role: str) -> None:
    """Update the user's role for a test scenario."""
    async with auth_db_logger.pool.acquire() as conn:
        await conn.execute("UPDATE users SET role = $1 WHERE id = $2", role, user_id)


async def _create_refresh_session(auth_db_logger, user_id: str, refresh_token: str) -> None:
    """Insert a valid refresh session for the given user."""
    async with auth_db_logger.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO auth_sessions (id, user_id, refresh_token_hash, jti, sid, expires_at, revoked)
            VALUES ($1, $2, $3, $4, $5, $6, FALSE)
            """,
            str(uuid4()),
            user_id,
            hash_refresh_token(refresh_token),
            str(uuid4()),
            str(uuid4()),
            datetime.now(timezone.utc) + timedelta(days=1),
        )


@pytest_asyncio.fixture
async def auth_test_user(auth_db_logger):
    """Create a user backed by the auth-specific DB fixtures."""
    if not auth_db_logger or not auth_db_logger.pool:
        pytest.skip("PostgreSQL auth test database is not available.")

    user_data = create_test_user()

    async with auth_db_logger.pool.acquire() as conn:
        await conn.execute("DELETE FROM email_verification_tokens")
        await conn.execute("DELETE FROM password_reset_tokens")
        await conn.execute("DELETE FROM auth_sessions")
        await conn.execute("DELETE FROM api_keys WHERE account_id IS NOT NULL")
        await conn.execute("DELETE FROM users")
        await conn.execute(
            """
            INSERT INTO users (id, email, password_hash, user_name, status, email_verified)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            user_data["id"],
            user_data["email"].lower(),
            user_data["password_hash"],
            user_data["user_name"],
            user_data["status"],
            user_data["email_verified"],
        )

    yield user_data

    async with auth_db_logger.pool.acquire() as conn:
        await conn.execute("DELETE FROM email_verification_tokens")
        await conn.execute("DELETE FROM password_reset_tokens")
        await conn.execute("DELETE FROM auth_sessions")
        await conn.execute("DELETE FROM api_keys WHERE account_id IS NOT NULL")
        await conn.execute("DELETE FROM users")


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
        auth_db_logger,
        auth_test_user,
    ) -> None:
        """Admin sessions should pass the auth_request check."""
        refresh_token = "test-refresh-admin"
        await _set_user_role(auth_db_logger, auth_test_user["id"], "admin")
        await _create_refresh_session(auth_db_logger, auth_test_user["id"], refresh_token)

        response = await internal_client.get(
            "/internal/verify-admin",
            cookies={"refresh_token": refresh_token},
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_verify_admin_rejects_internal_session(
        self,
        internal_client: AsyncClient,
        auth_db_logger,
        auth_test_user,
    ) -> None:
        """Internal sessions should not pass the admin auth_request check."""
        refresh_token = "test-refresh-internal"
        await _set_user_role(auth_db_logger, auth_test_user["id"], "internal")
        await _create_refresh_session(auth_db_logger, auth_test_user["id"], refresh_token)

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
        auth_db_logger,
    ) -> None:
        """Unknown refresh tokens should be rejected."""
        if not auth_db_logger or not auth_db_logger.pool:
            pytest.skip("PostgreSQL auth test database is not available.")

        response = await internal_client.get(
            "/internal/verify-admin",
            cookies={"refresh_token": "missing-session"},
        )

        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid session."
