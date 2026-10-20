"""Integration tests for the internal admin-check endpoint.

Run with: make test-db
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import AppServices
from serving.servers.routers import internal
from serving.servers.routers.auth_routes import hash_refresh_token

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
        test_user,
    ) -> None:
        """Admin sessions should pass the admin check."""
        operational_store, _, _, _ = auth_backend
        refresh_token = "test-refresh-admin"
        await _set_user_role(operational_store, test_user["id"], "admin")
        await _create_refresh_session(operational_store, test_user["id"], refresh_token)

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
        test_user,
    ) -> None:
        """Internal sessions should not pass the admin check."""
        operational_store, _, _, _ = auth_backend
        refresh_token = "test-refresh-internal"
        await _set_user_role(operational_store, test_user["id"], "internal")
        await _create_refresh_session(operational_store, test_user["id"], refresh_token)

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


class TestVerifyAdminRefusesWhatCannotOpenPgAdmin:
    """Every way a pre-browser-session cookie must fail the pgAdmin gate.

    pgAdmin is the whole database, and the console's proxy admits on this 200
    alone, so each refusal below is an access-control guarantee.
    """

    @staticmethod
    async def _admin_with_session(op_store, user_id: str, *, expires_in: timedelta) -> str:
        token = f"test-refresh-{uuid4()}"
        await _set_user_role(op_store, user_id, "admin")
        await op_store.create_session(
            session_id=str(uuid4()),
            user_id=user_id,
            refresh_token_hash=hash_refresh_token(token),
            jti=str(uuid4()),
            sid=str(uuid4()),
            expires_at=datetime.now(timezone.utc) + expires_in,
        )
        return token

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["suspended", "pending_approval", "rejected"])
    async def test_an_admin_who_is_not_active_is_refused(
        self, internal_client: AsyncClient, auth_backend, test_user, status
    ) -> None:
        """A suspended admin keeps a live session cookie; it must not open pgAdmin."""
        operational_store, _, _, _ = auth_backend
        token = await self._admin_with_session(
            operational_store, test_user["id"], expires_in=timedelta(days=1)
        )
        cookies = {"refresh_token": token}
        assert (
            await internal_client.get("/internal/verify-admin", cookies=cookies)
        ).status_code == 200

        await operational_store.update_user_fields(test_user["id"], status=status)

        response = await internal_client.get("/internal/verify-admin", cookies=cookies)
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_a_revoked_admin_session_is_refused(
        self, internal_client: AsyncClient, auth_backend, test_user
    ) -> None:
        operational_store, _, _, _ = auth_backend
        token = await self._admin_with_session(
            operational_store, test_user["id"], expires_in=timedelta(days=1)
        )
        await operational_store.revoke_user_sessions(test_user["id"])

        response = await internal_client.get(
            "/internal/verify-admin", cookies={"refresh_token": token}
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Session revoked."

    @pytest.mark.asyncio
    async def test_an_expired_admin_session_is_refused(
        self, internal_client: AsyncClient, auth_backend, test_user
    ) -> None:
        operational_store, _, _, _ = auth_backend
        token = await self._admin_with_session(
            operational_store, test_user["id"], expires_in=-timedelta(minutes=1)
        )

        response = await internal_client.get(
            "/internal/verify-admin", cookies={"refresh_token": token}
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Session expired."

    @pytest.mark.asyncio
    async def test_no_cookie_is_refused(self, internal_client: AsyncClient) -> None:
        response = await internal_client.get("/internal/verify-admin")
        assert response.status_code == 401
        assert response.json()["detail"] == "Not authenticated."

    @pytest.mark.asyncio
    async def test_a_user_without_a_role_is_not_an_admin(
        self, internal_client: AsyncClient, auth_backend, test_user
    ) -> None:
        operational_store, _, _, _ = auth_backend
        token = f"test-refresh-{uuid4()}"
        await _create_refresh_session(operational_store, test_user["id"], token)

        response = await internal_client.get(
            "/internal/verify-admin", cookies={"refresh_token": token}
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_without_a_database_it_fails_closed(self) -> None:
        app = FastAPI()
        app.state.services = AppServices(router=None, operational_store=None)
        app.include_router(internal.router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/internal/verify-admin", cookies={"refresh_token": "anything"}
            )
        assert response.status_code == 500
