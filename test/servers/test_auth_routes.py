"""Integration tests for authentication routes."""

import pytest
from httpx import AsyncClient

from test.fixtures.auth_factories import create_signup_request

# Import fixtures from conftest_auth
pytest_plugins = ["test.servers.conftest_auth"]


class TestSignup:
    """Test user signup endpoint."""

    @pytest.mark.asyncio
    async def test_signup_success(self, auth_app_client: AsyncClient, mock_email_service):
        """Test successful user signup."""
        signup_data = create_signup_request()

        response = await auth_app_client.post("/auth/signup", json=signup_data)

        assert response.status_code == 201
        data = response.json()
        assert data["email"] == signup_data["email"]
        assert "user_id" in data
        assert "password" not in data

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
    async def test_signup_missing_fields(self, auth_app_client: AsyncClient):
        """Test signup with missing required fields fails."""
        response = await auth_app_client.post("/auth/signup", json={})

        assert response.status_code == 422


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
