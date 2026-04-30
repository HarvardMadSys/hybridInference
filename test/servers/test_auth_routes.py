"""Integration tests for authentication routes."""

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


async def _set_user_role(auth_db_logger, user_id: str, role: str) -> None:
    """Update the user's role for a test scenario."""
    async with auth_db_logger.pool.acquire() as conn:
        await conn.execute("UPDATE users SET role = $1 WHERE id = $2", role, user_id)


async def _get_user_role(auth_db_logger, user_id: str) -> str:
    """Fetch the current role for a user."""
    async with auth_db_logger.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT role FROM users WHERE id = $1", user_id)
    return row["role"]


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
        assert data["email"] == signup_data["email"]
        assert "user_id" in data
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
        """Test signup with duplicate email fails."""
        signup_data = create_signup_request(email=test_user["email"])

        response = await auth_app_client.post("/auth/signup", json=signup_data)

        assert response.status_code == 409
        data = response.json()
        assert "already registered" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_signup_weak_password(self, auth_app_client: AsyncClient):
        """Test signup with weak password fails."""
        # Use a password that passes min_length but fails strength validation
        signup_data = create_signup_request(password="weakpass")  # 8 chars but no uppercase/number

        response = await auth_app_client.post("/auth/signup", json=signup_data)

        assert response.status_code == 400
        data = response.json()
        assert "password" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_signup_invalid_email(self, auth_app_client: AsyncClient):
        """Test signup with invalid email fails."""
        signup_data = create_signup_request(email="not-an-email")

        response = await auth_app_client.post("/auth/signup", json=signup_data)

        assert response.status_code == 422  # Validation error

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
        """Test signup with missing required fields fails."""
        response = await auth_app_client.post("/auth/signup", json={})

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

        # Space attempts > 1h/per_hour apart so the hour window only ever holds
        # one attempt at a time and the per-day limit is what eventually trips.
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
        assert "token_type" in data
        assert data["token_type"] == "bearer"

        # Check refresh token cookie
        assert "refresh_token" in response.cookies

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
        data = response.json()
        assert "invalid" in data["detail"].lower()

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
    async def test_login_inactive_user(
        self, auth_app_client: AsyncClient, auth_db_logger, test_user
    ):
        """Test login with inactive user fails."""
        # Deactivate user
        async with auth_db_logger.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET status = 'suspended' WHERE id = $1", test_user["id"]
            )

        response = await auth_app_client.post(
            "/auth/login",
            json={
                "email": test_user["email"],
                "password": test_user["password"],
            },
        )

        assert response.status_code == 403  # Forbidden for suspended account


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
        old_access_token = login_response.json()["access_token"]

        # Refresh
        response = await auth_app_client.post(
            "/auth/refresh", cookies={"refresh_token": refresh_token}
        )

        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["access_token"] != old_access_token  # New token

        # Check new refresh token cookie
        assert "refresh_token" in response.cookies

    @pytest.mark.asyncio
    async def test_refresh_without_token(self, auth_app_client: AsyncClient):
        """Test refresh without refresh token fails."""
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
        self, auth_app_client: AsyncClient, test_user, auth_db_logger
    ):
        """Test refresh with revoked session fails."""
        # Login
        login_response = await auth_app_client.post(
            "/auth/login",
            json={
                "email": test_user["email"],
                "password": test_user["password"],
            },
        )
        refresh_token = login_response.cookies.get("refresh_token")

        # Revoke session
        async with auth_db_logger.pool.acquire() as conn:
            await conn.execute(
                "UPDATE auth_sessions SET revoked = TRUE WHERE user_id = $1", test_user["id"]
            )

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
        auth_db_logger,
        auth_test_user,
        monkeypatch,
    ) -> None:
        """Login should promote matching free users to admin."""
        monkeypatch.setattr(settings_module.settings, "admin_emails", auth_test_user["email"])
        await _set_user_role(auth_db_logger, auth_test_user["id"], "free")

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
        assert await _get_user_role(auth_db_logger, auth_test_user["id"]) == "admin"

    @pytest.mark.asyncio
    async def test_login_bootstrap_does_not_repromote_non_free_user(
        self,
        auth_app_client: AsyncClient,
        auth_db_logger,
        auth_test_user,
        monkeypatch,
    ) -> None:
        """Login should not overwrite an explicitly assigned non-free role."""
        monkeypatch.setattr(settings_module.settings, "admin_emails", auth_test_user["email"])
        await _set_user_role(auth_db_logger, auth_test_user["id"], "internal")

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
        assert await _get_user_role(auth_db_logger, auth_test_user["id"]) == "internal"

    @pytest.mark.asyncio
    async def test_refresh_bootstrap_promotes_free_user_to_admin(
        self,
        auth_app_client: AsyncClient,
        auth_db_logger,
        auth_test_user,
        monkeypatch,
    ) -> None:
        """Refresh should promote matching free users to admin."""
        refresh_token = "test-refresh-bootstrap-admin"
        monkeypatch.setattr(settings_module.settings, "admin_emails", auth_test_user["email"])
        await _set_user_role(auth_db_logger, auth_test_user["id"], "free")
        await _create_refresh_session(auth_db_logger, auth_test_user["id"], refresh_token)

        response = await auth_app_client.post(
            "/auth/refresh",
            cookies={"refresh_token": refresh_token},
        )

        assert response.status_code == 200
        assert await _get_user_role(auth_db_logger, auth_test_user["id"]) == "admin"

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
        auth_db_logger,
        auth_test_user,
        monkeypatch,
    ) -> None:
        """Refresh should not overwrite an explicitly assigned non-free role."""
        refresh_token = "test-refresh-bootstrap-internal"
        monkeypatch.setattr(settings_module.settings, "admin_emails", auth_test_user["email"])
        await _set_user_role(auth_db_logger, auth_test_user["id"], "internal")
        await _create_refresh_session(auth_db_logger, auth_test_user["id"], refresh_token)

        response = await auth_app_client.post(
            "/auth/refresh",
            cookies={"refresh_token": refresh_token},
        )

        assert response.status_code == 200
        assert await _get_user_role(auth_db_logger, auth_test_user["id"]) == "internal"

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
        self, auth_app_client: AsyncClient, test_user, auth_db_logger
    ):
        """Test successful email verification."""
        import secrets
        from datetime import datetime, timedelta, timezone

        # Create verification token
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

        async with auth_db_logger.pool.acquire() as conn:
            # Mark user as unverified
            await conn.execute(
                "UPDATE users SET email_verified = FALSE WHERE id = $1", test_user["id"]
            )

            # Insert verification token
            await conn.execute(
                """
                INSERT INTO email_verification_tokens (token, user_id, expires_at)
                VALUES ($1, $2, $3)
                """,
                token,
                test_user["id"],
                expires_at,
            )

        # Verify email
        response = await auth_app_client.get(f"/auth/verify-email?token={token}")

        assert response.status_code == 200

        # Check user is verified
        async with auth_db_logger.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT email_verified FROM users WHERE id = $1", test_user["id"]
            )
            assert row["email_verified"] is True

    @pytest.mark.asyncio
    async def test_verify_email_invalid_token(self, auth_app_client: AsyncClient):
        """Test email verification with invalid token fails."""
        response = await auth_app_client.get("/auth/verify-email?token=invalid-token")

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_verify_email_expired_token(
        self, auth_app_client: AsyncClient, test_user, auth_db_logger
    ):
        """Test email verification with expired token fails."""
        import secrets
        from datetime import datetime, timedelta, timezone

        # Create expired token
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) - timedelta(hours=1)  # Expired

        async with auth_db_logger.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO email_verification_tokens (token, user_id, expires_at)
                VALUES ($1, $2, $3)
                """,
                token,
                test_user["id"],
                expires_at,
            )

        response = await auth_app_client.get(f"/auth/verify-email?token={token}")

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_verify_email_used_token(
        self, auth_app_client: AsyncClient, test_user, auth_db_logger
    ):
        """Test email verification with already used token fails."""
        import secrets
        from datetime import datetime, timedelta, timezone

        # Create used token
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

        async with auth_db_logger.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO email_verification_tokens (token, user_id, expires_at, used_at)
                VALUES ($1, $2, $3, NOW())
                """,
                token,
                test_user["id"],
                expires_at,
            )

        response = await auth_app_client.get(f"/auth/verify-email?token={token}")

        assert response.status_code == 400


class TestAuthFlow:
    """Test complete authentication flows."""

    @pytest.mark.asyncio
    async def test_complete_signup_login_flow(
        self, auth_app_client: AsyncClient, mock_email_service
    ):
        """Test complete flow: signup -> login -> access protected endpoint."""
        # 1. Signup
        signup_data = create_signup_request()
        signup_response = await auth_app_client.post("/auth/signup", json=signup_data)
        assert signup_response.status_code == 201

        # 2. Login
        login_response = await auth_app_client.post(
            "/auth/login",
            json={
                "email": signup_data["email"],
                "password": signup_data["password"],
            },
        )
        assert login_response.status_code == 200
        access_token = login_response.json()["access_token"]

        # 3. Access protected endpoint
        headers = {"Authorization": f"Bearer {access_token}"}
        me_response = await auth_app_client.get("/user/me", headers=headers)
        assert me_response.status_code == 200
        assert me_response.json()["email"] == signup_data["email"]

    @pytest.mark.asyncio
    async def test_login_refresh_flow(self, auth_app_client: AsyncClient, test_user):
        """Test flow: login -> refresh -> use new token."""
        # 1. Login
        login_response = await auth_app_client.post(
            "/auth/login",
            json={
                "email": test_user["email"],
                "password": test_user["password"],
            },
        )
        refresh_token = login_response.cookies.get("refresh_token")

        # 2. Refresh
        refresh_response = await auth_app_client.post(
            "/auth/refresh", cookies={"refresh_token": refresh_token}
        )
        assert refresh_response.status_code == 200
        new_access_token = refresh_response.json()["access_token"]

        # 3. Use new token
        headers = {"Authorization": f"Bearer {new_access_token}"}
        me_response = await auth_app_client.get("/user/me", headers=headers)
        assert me_response.status_code == 200
