"""Integration tests for authentication routes.

Supports both PostgreSQL and Cloudflare D1 backends.
Run with: make test-db
"""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient

import serving.config.settings as settings_module
from serving.servers.routers.auth_routes import hash_refresh_token
from test.fixtures.auth_factories import create_signup_request, create_test_user

# Import fixtures from conftest_auth
pytest_plugins = ["test.servers.conftest_auth"]

pytestmark = pytest.mark.dbtest


# ---------------------------------------------------------------------------
# Helpers — use the store abstraction, not raw SQL
# ---------------------------------------------------------------------------


async def _set_user_role(op_store, user_id: str, role: str) -> None:
    """Update the user's role for a test scenario."""
    await op_store.update_user_fields(user_id, role=role)


async def _get_user_role(op_store, user_id: str) -> str:
    """Fetch the current role for a user."""
    user = await op_store.get_user_by_id(user_id)
    return user["role"]


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


class TestSignup:
    """Test user signup endpoint."""

    @pytest.mark.asyncio
    async def test_signup_success(
        self, auth_app_client: AsyncClient, mock_email_service, auth_db_logger
    ):
        """Test successful user signup."""
        signup_data = create_signup_request()

        response = await auth_app_client.post("/auth/signup", json=signup_data)

        assert response.status_code == 201
        data = response.json()
        assert data["email"] == signup_data["email"].lower()
        assert "password" not in data

        async with auth_db_logger.pool.acquire() as conn:
            audit_row = await conn.fetchrow(
                """
                SELECT action, target_user_id, details, success
                FROM admin_audit_log
                WHERE target_user_id = $1 AND action = 'create_user'
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                data["user_id"],
            )
        assert audit_row is not None
        assert audit_row["success"] is True
        details = audit_row["details"]
        if isinstance(details, str):
            details = json.loads(details)
        assert details["email"] == signup_data["email"].lower()
        assert "password" not in details

    @pytest.mark.asyncio
    async def test_signup_duplicate_email(
        self, auth_app_client: AsyncClient, test_user, mock_email_service
    ):
        """Test signup with existing email fails."""
        response = await auth_app_client.post(
            "/auth/signup",
            json={
                "email": test_user["email"],
                "password": "SecurePass123!",
                "user_name": "Duplicate User",
            },
        )

        assert response.status_code == 409  # Conflict

    @pytest.mark.asyncio
    async def test_signup_weak_password(self, auth_app_client: AsyncClient):
        """Test signup with weak password fails."""
        response = await auth_app_client.post(
            "/auth/signup",
            json={
                "email": "weakpass@example.com",
                "password": "123",
                "user_name": "Weak Pass",
            },
        )

        assert response.status_code == 422  # Validation error

    @pytest.mark.asyncio
    async def test_signup_invalid_email(self, auth_app_client: AsyncClient):
        """Test signup with invalid email format fails."""
        response = await auth_app_client.post(
            "/auth/signup",
            json={
                "email": "not-an-email",
                "password": "SecurePass123!",
                "user_name": "Invalid Email",
            },
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_signup_missing_username(self, auth_app_client: AsyncClient):
        """Test signup without username fails."""
        signup_data = create_signup_request()
        signup_data.pop("user_name")

        response = await auth_app_client.post("/auth/signup", json=signup_data)

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_signup_blank_username(self, auth_app_client: AsyncClient):
        """Test signup with blank username fails."""
        signup_data = create_signup_request(user_name="  ")

        response = await auth_app_client.post("/auth/signup", json=signup_data)

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_signup_missing_fields(self, auth_app_client: AsyncClient):
        """Test signup with missing fields fails."""
        response = await auth_app_client.post(
            "/auth/signup",
            json={"email": "missing@example.com"},
        )

        assert response.status_code == 422


class TestSignupAbuseProtection:
    """Rate limit, captcha, and email-domain blocklist on /auth/signup."""

    @pytest.mark.asyncio
    async def test_signup_rate_limited_per_hour(
        self, auth_app_client: AsyncClient, mock_email_service
    ):
        for _ in range(5):
            response = await auth_app_client.post("/auth/signup", json=create_signup_request())
            assert response.status_code in (201, 409)

        response = await auth_app_client.post("/auth/signup", json=create_signup_request())
        assert response.status_code == 429
        assert "Retry-After" in response.headers

    @pytest.mark.asyncio
    async def test_signup_rate_limited_per_day(
        self, auth_app_client: AsyncClient, mock_email_service, monkeypatch
    ):
        from serving.utils import signup_rate_limit

        # Space attempts 1000s apart: that's > 3600s / per_hour (720s for a
        # 5/hour budget), so each rolling hour window holds < per_hour
        # attempts and the per-day limit is what eventually trips.
        base = signup_rate_limit._now()
        spacing = 1000
        offsets = iter([base + i * spacing for i in range(20)])
        monkeypatch.setattr(signup_rate_limit, "_now", lambda: next(offsets))

        for _ in range(10):
            response = await auth_app_client.post("/auth/signup", json=create_signup_request())
            assert response.status_code in (201, 409)

        response = await auth_app_client.post("/auth/signup", json=create_signup_request())
        assert response.status_code == 429
        assert response.headers.get("Retry-After") == "86400"

    @pytest.mark.asyncio
    async def test_signup_blocked_domain_example_com(self, auth_app_client: AsyncClient):
        signup_data = create_signup_request(email="someone@example.com")
        response = await auth_app_client.post("/auth/signup", json=signup_data)
        assert response.status_code == 400
        detail = response.json()["detail"].lower()
        assert "domain" in detail
        assert "example.com" not in detail

    @pytest.mark.asyncio
    async def test_signup_blocked_reserved_tld_test(self, auth_app_client: AsyncClient):
        # Pydantic's EmailStr already rejects RFC-2606 reserved TLDs at the
        # validation layer (HTTP 422). The blocklist is defense-in-depth: even
        # if Pydantic relaxes, the helper must reject these directly.
        from serving.utils.email_blocklist import is_email_domain_blocked

        assert is_email_domain_blocked("foo@bar.test")
        assert is_email_domain_blocked("foo@bar.example")
        assert is_email_domain_blocked("foo@bar.invalid")
        assert is_email_domain_blocked("foo@bar.localhost")

        signup_data = create_signup_request(email="foo@bar.test")
        response = await auth_app_client.post("/auth/signup", json=signup_data)
        assert response.status_code in (400, 422)

    @pytest.mark.asyncio
    async def test_signup_turnstile_missing_when_required(
        self, auth_app_client: AsyncClient, monkeypatch
    ):
        monkeypatch.setenv("TURNSTILE_SECRET_KEY", "test-secret")
        signup_data = create_signup_request()
        response = await auth_app_client.post("/auth/signup", json=signup_data)
        assert response.status_code == 400
        assert "captcha" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_signup_turnstile_valid(
        self,
        auth_app_client: AsyncClient,
        mock_email_service,
        monkeypatch,
    ):
        monkeypatch.setenv("TURNSTILE_SECRET_KEY", "test-secret")

        async def fake_verify(token, remote_ip):
            return token == "valid-token"

        monkeypatch.setattr(
            "serving.servers.routers.auth_routes.verify_turnstile_token", fake_verify
        )

        signup_data = create_signup_request()
        signup_data["turnstile_token"] = "valid-token"
        response = await auth_app_client.post("/auth/signup", json=signup_data)
        assert response.status_code == 201


class TestLogin:
    """Test user login endpoint."""

    @pytest.mark.asyncio
    async def test_login_success(self, auth_app_client: AsyncClient, test_user):
        """Test successful login."""
        response = await auth_app_client.post(
            "/auth/login",
            json={
                "email": test_user["email"],
                "password": test_user["password"],
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["user"]["email"] == test_user["email"].lower()

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, auth_app_client: AsyncClient, test_user):
        """Test login with wrong password fails."""
        response = await auth_app_client.post(
            "/auth/login",
            json={
                "email": test_user["email"],
                "password": "WrongPassword123!",
            },
        )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_login_nonexistent_user(self, auth_app_client: AsyncClient):
        """Test login with nonexistent user fails."""
        response = await auth_app_client.post(
            "/auth/login",
            json={
                "email": "nonexistent@example.com",
                "password": "SomePassword123!",
            },
        )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_login_inactive_user(self, auth_app_client: AsyncClient, auth_backend, test_user):
        """Test login with inactive user fails."""
        operational_store, _, _, _ = auth_backend
        await operational_store.update_user_fields(test_user["id"], status="suspended")

        response = await auth_app_client.post(
            "/auth/login",
            json={
                "email": test_user["email"],
                "password": test_user["password"],
            },
        )

        assert response.status_code == 403  # Forbidden for suspended account


class TestLoginAbuseProtection:
    """Per-email and per-IP rate limits on /auth/login."""

    @pytest.mark.asyncio
    async def test_login_rate_limited_per_email(
        self, auth_app_client: AsyncClient, test_user
    ):
        """Per-email bucket trips after `login_rate_limit_per_15min` attempts.

        Default is 5: after 5 wrong-password attempts within 15 minutes,
        a 6th attempt against the same email returns 429 with Retry-After.
        """
        for _ in range(5):
            response = await auth_app_client.post(
                "/auth/login",
                json={"email": test_user["email"], "password": "WrongPassword123!"},
            )
            assert response.status_code == 401

        response = await auth_app_client.post(
            "/auth/login",
            json={"email": test_user["email"], "password": "WrongPassword123!"},
        )
        assert response.status_code == 429
        assert "Retry-After" in response.headers

    @pytest.mark.asyncio
    async def test_login_rate_limited_per_ip(self, auth_app_client: AsyncClient):
        """Per-IP bucket trips at `login_rate_limit_per_hour_per_ip`.

        Vary the email each attempt so the per-email bucket cannot trip;
        the per-IP limit (default 20) must be the gating factor.
        """
        for i in range(20):
            response = await auth_app_client.post(
                "/auth/login",
                json={
                    "email": f"unique-{i}@example.com",
                    "password": "AnyPassword123!",
                },
            )
            assert response.status_code in (401, 403)

        response = await auth_app_client.post(
            "/auth/login",
            json={"email": "next@example.com", "password": "AnyPassword123!"},
        )
        assert response.status_code == 429
        assert response.headers.get("Retry-After") == "3600"


class TestLogout:
    """Test user logout endpoint."""

    @pytest.mark.asyncio
    async def test_logout_success(self, auth_app_client: AsyncClient, test_user, auth_headers):
        """Test successful logout."""
        # First login to get refresh token
        login_response = await auth_app_client.post(
            "/auth/login",
            json={
                "email": test_user["email"],
                "password": test_user["password"],
            },
        )
        refresh_token = login_response.cookies.get("refresh_token")

        # Logout
        response = await auth_app_client.post(
            "/auth/logout", headers=auth_headers, cookies={"refresh_token": refresh_token}
        )

        assert response.status_code == 200
        data = response.json()
        assert "logged out" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_logout_without_auth(self, auth_app_client: AsyncClient):
        """Test logout without authentication fails."""
        response = await auth_app_client.post("/auth/logout")

        assert response.status_code == 401


class TestRefreshToken:
    """Test token refresh endpoint."""

    @pytest.mark.asyncio
    async def test_refresh_success(self, auth_app_client: AsyncClient, test_user):
        """Test successful token refresh."""
        # Login to get refresh token
        login_response = await auth_app_client.post(
            "/auth/login",
            json={
                "email": test_user["email"],
                "password": test_user["password"],
            },
        )
        refresh_token = login_response.cookies.get("refresh_token")

        # Refresh
        response = await auth_app_client.post(
            "/auth/refresh", cookies={"refresh_token": refresh_token}
        )

        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data

    @pytest.mark.asyncio
    async def test_refresh_rotates_cookie(self, auth_app_client: AsyncClient, test_user):
        """Refresh must mint a NEW refresh-token cookie and invalidate the old.

        Token rotation is the mitigation against a stolen refresh token:
        once the legitimate client refreshes, the attacker's copy stops
        working (because the stored hash has changed in op_store).
        """
        login_response = await auth_app_client.post(
            "/auth/login",
            json={"email": test_user["email"], "password": test_user["password"]},
        )
        original_refresh = login_response.cookies.get("refresh_token")
        assert original_refresh

        # First refresh: should rotate.
        first_refresh_response = await auth_app_client.post(
            "/auth/refresh", cookies={"refresh_token": original_refresh}
        )
        assert first_refresh_response.status_code == 200
        rotated_refresh = first_refresh_response.cookies.get("refresh_token")
        assert rotated_refresh, "expected new refresh_token cookie on /auth/refresh"
        assert rotated_refresh != original_refresh, "refresh token was not rotated"

        # Original refresh token must no longer be accepted.
        replay_response = await auth_app_client.post(
            "/auth/refresh", cookies={"refresh_token": original_refresh}
        )
        assert replay_response.status_code == 401

        # New refresh token still works.
        followup_response = await auth_app_client.post(
            "/auth/refresh", cookies={"refresh_token": rotated_refresh}
        )
        assert followup_response.status_code == 200

    @pytest.mark.asyncio
    async def test_refresh_without_token(self, auth_app_client: AsyncClient):
        """Test refresh without token fails."""
        response = await auth_app_client.post("/auth/refresh")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_invalid_token(self, auth_app_client: AsyncClient):
        """Test refresh with invalid token fails."""
        response = await auth_app_client.post(
            "/auth/refresh", cookies={"refresh_token": "invalid-token"}
        )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_revoked_session(
        self, auth_app_client: AsyncClient, test_user, auth_backend
    ):
        """Test refresh with revoked session fails."""
        operational_store, _, _, _ = auth_backend

        # Login
        login_response = await auth_app_client.post(
            "/auth/login",
            json={
                "email": test_user["email"],
                "password": test_user["password"],
            },
        )
        refresh_token = login_response.cookies.get("refresh_token")

        # Revoke all sessions for the user
        await operational_store.delete_user_sessions(test_user["id"])

        # Try to refresh
        response = await auth_app_client.post(
            "/auth/refresh", cookies={"refresh_token": refresh_token}
        )

        assert response.status_code == 401


class TestRoleBootstrap:
    """Test bootstrap promotion from ADMIN_EMAILS."""

    @pytest.mark.asyncio
    async def test_login_bootstrap_promotes_free_user_to_admin(
        self,
        auth_app_client: AsyncClient,
        auth_backend,
        auth_test_user,
        monkeypatch,
    ) -> None:
        """Login should promote matching free users to admin."""
        operational_store, _, _, _ = auth_backend
        monkeypatch.setattr(settings_module.settings, "admin_emails", auth_test_user["email"])
        await _set_user_role(operational_store, auth_test_user["id"], "free")

        response = await auth_app_client.post(
            "/auth/login",
            json={
                "email": auth_test_user["email"],
                "password": auth_test_user["password"],
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["user"]["role"] == "admin"
        assert data["user"]["is_admin"] is True
        assert await _get_user_role(operational_store, auth_test_user["id"]) == "admin"

    @pytest.mark.asyncio
    async def test_login_bootstrap_does_not_repromote_non_free_user(
        self,
        auth_app_client: AsyncClient,
        auth_backend,
        auth_test_user,
        monkeypatch,
    ) -> None:
        """Login should not overwrite an explicitly assigned non-free role."""
        operational_store, _, _, _ = auth_backend
        monkeypatch.setattr(settings_module.settings, "admin_emails", auth_test_user["email"])
        await _set_user_role(operational_store, auth_test_user["id"], "internal")

        response = await auth_app_client.post(
            "/auth/login",
            json={
                "email": auth_test_user["email"],
                "password": auth_test_user["password"],
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["user"]["role"] == "internal"
        assert data["user"]["is_admin"] is False
        assert await _get_user_role(operational_store, auth_test_user["id"]) == "internal"

    @pytest.mark.asyncio
    async def test_refresh_bootstrap_promotes_free_user_to_admin(
        self,
        auth_app_client: AsyncClient,
        auth_backend,
        auth_test_user,
        monkeypatch,
    ) -> None:
        """Refresh should promote matching free users to admin."""
        operational_store, _, _, _ = auth_backend
        refresh_token = "test-refresh-bootstrap-admin"
        monkeypatch.setattr(settings_module.settings, "admin_emails", auth_test_user["email"])
        await _set_user_role(operational_store, auth_test_user["id"], "free")
        await _create_refresh_session(operational_store, auth_test_user["id"], refresh_token)

        response = await auth_app_client.post(
            "/auth/refresh",
            cookies={"refresh_token": refresh_token},
        )

        assert response.status_code == 200
        assert await _get_user_role(operational_store, auth_test_user["id"]) == "admin"

        me_response = await auth_app_client.get(
            "/user/me",
            headers={"Authorization": f"Bearer {response.json()['access_token']}"},
        )
        assert me_response.status_code == 200
        assert me_response.json()["role"] == "admin"
        assert me_response.json()["is_admin"] is True

    @pytest.mark.asyncio
    async def test_refresh_bootstrap_does_not_repromote_non_free_user(
        self,
        auth_app_client: AsyncClient,
        auth_backend,
        auth_test_user,
        monkeypatch,
    ) -> None:
        """Refresh should not overwrite an explicitly assigned non-free role."""
        operational_store, _, _, _ = auth_backend
        refresh_token = "test-refresh-bootstrap-internal"
        monkeypatch.setattr(settings_module.settings, "admin_emails", auth_test_user["email"])
        await _set_user_role(operational_store, auth_test_user["id"], "internal")
        await _create_refresh_session(operational_store, auth_test_user["id"], refresh_token)

        response = await auth_app_client.post(
            "/auth/refresh",
            cookies={"refresh_token": refresh_token},
        )

        assert response.status_code == 200
        assert await _get_user_role(operational_store, auth_test_user["id"]) == "internal"

        me_response = await auth_app_client.get(
            "/user/me",
            headers={"Authorization": f"Bearer {response.json()['access_token']}"},
        )
        assert me_response.status_code == 200
        assert me_response.json()["role"] == "internal"
        assert me_response.json()["is_admin"] is False


class TestEmailVerification:
    """Test email verification endpoint."""

    @pytest.mark.asyncio
    async def test_verify_email_success(
        self, auth_app_client: AsyncClient, test_user, auth_backend
    ):
        """Test successful email verification."""
        import secrets

        operational_store, _, _, _ = auth_backend

        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

        # Mark user as unverified
        await operational_store.update_user_fields(test_user["id"], email_verified=False)

        # Insert verification token
        await operational_store.create_verification_token(
            token=token,
            user_id=test_user["id"],
            expires_at=expires_at,
        )

        # Verify email
        response = await auth_app_client.get(f"/auth/verify-email?token={token}")

        assert response.status_code == 200

        # Check user is verified
        user = await operational_store.get_user_by_id(test_user["id"])
        assert user["email_verified"] is True or user["email_verified"] == 1

    @pytest.mark.asyncio
    async def test_verify_email_invalid_token(self, auth_app_client: AsyncClient):
        """Test email verification with invalid token fails."""
        response = await auth_app_client.get("/auth/verify-email?token=invalid-token")

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_verify_email_expired_token(
        self, auth_app_client: AsyncClient, test_user, auth_backend
    ):
        """Test email verification with expired token fails."""
        import secrets

        operational_store, _, _, _ = auth_backend

        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) - timedelta(hours=1)  # Expired

        await operational_store.create_verification_token(
            token=token,
            user_id=test_user["id"],
            expires_at=expires_at,
        )

        response = await auth_app_client.get(f"/auth/verify-email?token={token}")

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_verify_email_used_token(
        self, auth_app_client: AsyncClient, test_user, auth_backend
    ):
        """Test email verification with already used token fails."""
        import secrets

        operational_store, _, _, _ = auth_backend

        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

        await operational_store.create_verification_token(
            token=token,
            user_id=test_user["id"],
            expires_at=expires_at,
        )

        # Mark as used
        await operational_store.mark_verification_used(token)

        response = await auth_app_client.get(f"/auth/verify-email?token={token}")

        assert response.status_code == 400


class TestAuthFlow:
    """Test complete authentication flows."""

    @pytest.mark.asyncio
    async def test_complete_signup_login_flow(
        self, auth_app_client: AsyncClient, mock_email_service
    ):
        """Test complete signup -> login flow."""
        signup_data = create_signup_request()

        # Signup
        signup_response = await auth_app_client.post("/auth/signup", json=signup_data)
        assert signup_response.status_code == 201

        # Login
        login_response = await auth_app_client.post(
            "/auth/login",
            json={
                "email": signup_data["email"],
                "password": signup_data["password"],
            },
        )
        assert login_response.status_code == 200
        data = login_response.json()
        assert "access_token" in data

    @pytest.mark.asyncio
    async def test_login_refresh_flow(self, auth_app_client: AsyncClient, test_user):
        """Test login -> refresh -> access flow."""
        # Login
        login_response = await auth_app_client.post(
            "/auth/login",
            json={
                "email": test_user["email"],
                "password": test_user["password"],
            },
        )
        assert login_response.status_code == 200
        refresh_token = login_response.cookies.get("refresh_token")

        # Refresh
        refresh_response = await auth_app_client.post(
            "/auth/refresh", cookies={"refresh_token": refresh_token}
        )
        assert refresh_response.status_code == 200

        # Access protected endpoint with new token
        new_token = refresh_response.json()["access_token"]
        me_response = await auth_app_client.get(
            "/user/me", headers={"Authorization": f"Bearer {new_token}"}
        )
        assert me_response.status_code == 200
