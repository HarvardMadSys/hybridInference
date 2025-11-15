"""Integration tests for user routes."""

import pytest
from httpx import AsyncClient

# Import fixtures from conftest_auth
pytest_plugins = ["test.servers.conftest_auth"]


class TestUserInfo:
    """Test user info endpoint."""

    @pytest.mark.asyncio
    async def test_get_user_me_success(self, auth_app_client: AsyncClient, test_user, auth_headers):
        """Test getting current user info."""
        response = await auth_app_client.get("/user/me", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == test_user["id"]
        assert data["email"] == test_user["email"].lower()  # Email is stored in lowercase
        assert data["user_name"] == test_user["user_name"]
        assert data["status"] == test_user["status"]
        assert data["email_verified"] == test_user["email_verified"]
        assert "password" not in data
        assert "password_hash" not in data

    @pytest.mark.asyncio
    async def test_get_user_me_without_auth(self, auth_app_client: AsyncClient):
        """Test getting user info without authentication fails."""
        response = await auth_app_client.get("/user/me")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_get_user_me_invalid_token(self, auth_app_client: AsyncClient):
        """Test getting user info with invalid token fails."""
        headers = {"Authorization": "Bearer invalid-token"}
        response = await auth_app_client.get("/user/me", headers=headers)

        assert response.status_code == 401


class TestAPIKeyManagement:
    """Test API key management endpoints."""

    @pytest.mark.asyncio
    async def test_create_api_key_success(
        self, auth_app_client: AsyncClient, test_user, auth_headers
    ):
        """Test creating API key."""
        response = await auth_app_client.post("/user/api-keys", headers=auth_headers)

        assert response.status_code == 201
        data = response.json()
        assert "api_key" in data
        assert data["api_key"].startswith("hyi-")  # Changed from sk-
        assert len(data["api_key"]) > 20
        assert "key_prefix" in data

    @pytest.mark.asyncio
    async def test_create_duplicate_api_key(
        self, auth_app_client: AsyncClient, test_user_with_key, auth_headers
    ):
        """Test creating duplicate API key fails."""
        response = await auth_app_client.post("/user/api-keys", headers=auth_headers)

        assert response.status_code == 409
        data = response.json()
        assert "already" in data["detail"].lower()  # More flexible matching

    @pytest.mark.asyncio
    async def test_get_api_key_info_success(
        self, auth_app_client: AsyncClient, test_user_with_key, auth_headers
    ):
        """Test getting API key info."""
        response = await auth_app_client.get("/user/api-keys", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()
        assert "key_prefix" in data
        assert "key_masked" in data
        assert data["key_prefix"] == test_user_with_key["key_prefix"]
        assert "*" in data["key_masked"]  # Should be masked
        assert "created_at" in data
        assert "status" in data

    @pytest.mark.asyncio
    async def test_get_api_key_info_no_key(
        self, auth_app_client: AsyncClient, test_user, auth_headers
    ):
        """Test getting API key info when user has no key."""
        response = await auth_app_client.get("/user/api-keys", headers=auth_headers)

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_regenerate_api_key_success(
        self, auth_app_client: AsyncClient, test_user_with_key, auth_headers
    ):
        """Test regenerating API key."""
        old_key_prefix = test_user_with_key["key_prefix"]

        response = await auth_app_client.post("/user/api-keys/regenerate", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()
        assert "api_key" in data  # Changed from new_api_key
        assert "old_key_prefix" in data
        assert data["old_key_prefix"] == old_key_prefix
        assert data["api_key"].startswith("hyi-")  # Changed from new_api_key
        assert data["api_key"] != test_user_with_key["api_key"]  # Changed from new_api_key

    @pytest.mark.asyncio
    async def test_regenerate_api_key_no_existing_key(
        self, auth_app_client: AsyncClient, test_user, auth_headers
    ):
        """Test regenerating API key when user has no key fails."""
        response = await auth_app_client.post("/user/api-keys/regenerate", headers=auth_headers)

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_regenerate_api_key_transaction(
        self, auth_app_client: AsyncClient, test_user_with_key, auth_headers, auth_db_logger
    ):
        """Test that regenerate uses transaction (old key revoked, new key created)."""
        old_key_prefix = test_user_with_key["key_prefix"]

        # Regenerate
        response = await auth_app_client.post("/user/api-keys/regenerate", headers=auth_headers)
        assert response.status_code == 200

        # Check database state
        async with auth_db_logger.pool.acquire() as conn:
            # Old key should be revoked
            old_key = await conn.fetchrow(
                "SELECT status FROM api_keys WHERE key_prefix = $1", old_key_prefix
            )
            assert old_key["status"] == "revoked"

            # New key should be active
            active_keys = await conn.fetch(
                "SELECT * FROM api_keys WHERE account_id = $1 AND status = 'active'",
                test_user_with_key["id"],
            )
            assert len(active_keys) == 1


class TestUsageStatistics:
    """Test usage statistics endpoint."""

    @pytest.mark.asyncio
    async def test_get_usage_success(
        self, auth_app_client: AsyncClient, test_user_with_key, auth_headers
    ):
        """Test getting usage statistics."""
        response = await auth_app_client.get("/user/usage", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()
        # Check nested structure
        assert "usage" in data
        assert "quota" in data
        assert "period" in data
        # Check usage fields
        assert "requests" in data["usage"]
        assert "cost_usd" in data["usage"]
        assert isinstance(data["usage"]["requests"], int)
        assert isinstance(data["usage"]["cost_usd"], int | float)
        # Check quota fields
        assert "daily_limit_usd" in data["quota"]

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Requires request_logs table from full logging system")
    async def test_get_usage_with_data(
        self, auth_app_client: AsyncClient, test_user_with_key, auth_headers, auth_db_logger
    ):
        """Test getting usage statistics with actual usage data."""
        # Insert some usage data
        async with auth_db_logger.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO request_logs (
                    api_key_id, model, prompt_tokens, completion_tokens,
                    total_tokens, cost_usd, status
                )
                SELECT id, 'test-model', 100, 50, 150, 0.01, 'success'
                FROM api_keys
                WHERE key_prefix = $1
                """,
                test_user_with_key["key_prefix"],
            )

        response = await auth_app_client.get("/user/usage", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["total_requests"] >= 1
        assert data["total_cost_usd"] >= 0.01


class TestUserProfile:
    """Test user profile update endpoint."""

    @pytest.mark.asyncio
    async def test_update_profile_success(
        self, auth_app_client: AsyncClient, test_user, auth_headers
    ):
        """Test updating user profile."""
        new_name = "Updated Name"

        response = await auth_app_client.patch(
            "/user/profile", headers=auth_headers, json={"user_name": new_name}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["user_name"] == new_name

    @pytest.mark.asyncio
    async def test_update_profile_empty_update(
        self, auth_app_client: AsyncClient, test_user, auth_headers
    ):
        """Test updating profile with no changes."""
        response = await auth_app_client.patch("/user/profile", headers=auth_headers, json={})

        assert response.status_code == 400


class TestConcurrentAPIKeyCreation:
    """Test concurrent API key creation (database constraint)."""

    @pytest.mark.asyncio
    async def test_concurrent_key_creation_prevented(
        self, auth_app_client: AsyncClient, test_user, auth_headers, auth_db_logger
    ):
        """Test that concurrent key creation is prevented by database constraint.

        Note: This test assumes the unique constraint is in place.
        """
        import asyncio

        # Try to create two keys concurrently
        tasks = [
            auth_app_client.post("/user/api-keys", headers=auth_headers),
            auth_app_client.post("/user/api-keys", headers=auth_headers),
        ]

        responses = await asyncio.gather(*tasks, return_exceptions=True)

        # One should succeed, one should fail
        status_codes = [r.status_code for r in responses if hasattr(r, "status_code")]

        # At least one should succeed
        assert 201 in status_codes

        # At least one should fail with 409 (or both if timing is perfect)
        # In practice, one will succeed and one will fail
        success_count = status_codes.count(201)
        assert success_count == 1, "Only one concurrent key creation should succeed"

        # Verify only one active key exists
        async with auth_db_logger.pool.acquire() as conn:
            active_keys = await conn.fetch(
                "SELECT * FROM api_keys WHERE account_id = $1 AND status = 'active'",
                test_user["id"],
            )
            assert len(active_keys) == 1, "Only one active key should exist"


class TestAuthenticationEdgeCases:
    """Test edge cases in authentication."""

    @pytest.mark.asyncio
    async def test_expired_token(self, auth_app_client: AsyncClient, test_user, auth_env):
        """Test that expired access token is rejected."""
        from datetime import timedelta

        from serving.utils.jwt import create_access_token

        # Create an expired token
        expired_token, _ = create_access_token(
            user_id=test_user["id"],
            email=test_user["email"],
            tier="free",
            expires_delta=timedelta(seconds=-1),  # Already expired
        )

        headers = {"Authorization": f"Bearer {expired_token}"}
        response = await auth_app_client.get("/user/me", headers=headers)

        assert response.status_code == 401
        assert "expired" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_suspended_user_cannot_access(
        self, auth_app_client: AsyncClient, test_user, auth_headers, auth_db_logger
    ):
        """Test that suspended user cannot access protected endpoints."""
        # Suspend user
        async with auth_db_logger.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET status = 'suspended' WHERE id = $1", test_user["id"]
            )

        response = await auth_app_client.get("/user/me", headers=auth_headers)

        assert response.status_code == 403
        assert "suspended" in response.json()["detail"].lower()
