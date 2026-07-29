"""Integration tests for user routes.

Run with: make test-db
"""

import json

import pytest
from httpx import AsyncClient

# Import fixtures from conftest_auth
pytest_plugins = ["tests.servers.conftest_auth"]

pytestmark = pytest.mark.dbtest


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
        self, auth_app_client: AsyncClient, test_user, auth_headers, auth_db_logger
    ):
        """Test creating API key."""
        response = await auth_app_client.post("/user/api-keys", headers=auth_headers)

        assert response.status_code == 201
        data = response.json()
        assert "api_key" in data
        assert data["api_key"].startswith("hyi-")  # Changed from sk-
        assert len(data["api_key"]) > 20
        assert "key_prefix" in data

        async with auth_db_logger.pool.acquire() as conn:
            audit_row = await conn.fetchrow(
                """
                SELECT action, details, success
                FROM admin_audit_log
                WHERE target_user_id = $1 AND action = 'create_key'
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                test_user["id"],
            )
        assert audit_row is not None
        assert audit_row["success"] is True
        details = audit_row["details"]
        if isinstance(details, str):
            details = json.loads(details)
        assert details["actor"] == "user"
        assert details["key_prefix"] == data["key_prefix"]
        assert data["api_key"] not in json.dumps(details)

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
        assert data["api_key"] is None
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
    async def test_list_api_keys_no_key(
        self, auth_app_client: AsyncClient, test_user, auth_headers
    ):
        """Test listing API keys when user has no key."""
        response = await auth_app_client.get("/user/api-keys/all", headers=auth_headers)

        assert response.status_code == 200
        assert response.json() == {"keys": []}

    @pytest.mark.asyncio
    async def test_list_api_keys_success(
        self, auth_app_client: AsyncClient, test_user_with_key, auth_headers, auth_db_logger
    ):
        """Test listing active and revoked API key records."""
        async with auth_db_logger.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE api_keys
                SET status = 'revoked'
                WHERE key_prefix = $1
                """,
                test_user_with_key["key_prefix"],
            )

        response = await auth_app_client.post("/user/api-keys", headers=auth_headers)
        assert response.status_code == 201

        response = await auth_app_client.get("/user/api-keys/all", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()
        assert len(data["keys"]) == 2
        assert data["keys"][0]["status"] == "active"
        assert data["keys"][1]["status"] == "revoked"
        assert data["keys"][1]["api_key"] is None
        assert "key_masked" in data["keys"][0]
        assert "*" in data["keys"][0]["key_masked"]

    @pytest.mark.asyncio
    async def test_list_api_keys_reveals_full_key_for_active_key(
        self, auth_app_client: AsyncClient, test_user, auth_headers
    ):
        """A key created after this feature exposes the decrypted full key.

        The plaintext round-trips through the stored ciphertext
        (``encrypt`` at create time -> DB -> ``decrypt`` at list time).
        """
        create = await auth_app_client.post("/user/api-keys", headers=auth_headers)
        assert create.status_code == 201
        created_key = create.json()["api_key"]

        response = await auth_app_client.get("/user/api-keys/all", headers=auth_headers)
        assert response.status_code == 200
        keys = response.json()["keys"]
        assert len(keys) == 1
        assert keys[0]["status"] == "active"
        assert keys[0]["api_key"] == created_key
        assert keys[0]["api_key"].startswith("hyi-")

    @pytest.mark.asyncio
    async def test_list_api_keys_revoked_key_stays_masked(
        self, auth_app_client: AsyncClient, test_user, auth_headers, auth_db_logger
    ):
        """A revoked key returns ``api_key=None`` even though ciphertext exists."""
        create = await auth_app_client.post("/user/api-keys", headers=auth_headers)
        assert create.status_code == 201
        key_prefix = create.json()["key_prefix"]

        async with auth_db_logger.pool.acquire() as conn:
            await conn.execute(
                "UPDATE api_keys SET status = 'revoked' WHERE key_prefix = $1",
                key_prefix,
            )
            # Sanity check: ciphertext is present, so masking is driven by status.
            encrypted = await conn.fetchval(
                "SELECT api_key_encrypted FROM api_keys WHERE key_prefix = $1",
                key_prefix,
            )
        assert encrypted is not None

        response = await auth_app_client.get("/user/api-keys/all", headers=auth_headers)
        assert response.status_code == 200
        keys = response.json()["keys"]
        assert len(keys) == 1
        assert keys[0]["status"] == "revoked"
        assert keys[0]["api_key"] is None
        assert "*" in keys[0]["key_masked"]

    @pytest.mark.asyncio
    async def test_list_api_keys_legacy_key_masked(
        self, auth_app_client: AsyncClient, test_user_with_key, auth_headers
    ):
        """A legacy active key (``api_key_encrypted`` NULL) stays masked."""
        response = await auth_app_client.get("/user/api-keys/all", headers=auth_headers)
        assert response.status_code == 200
        keys = response.json()["keys"]
        assert len(keys) == 1
        assert keys[0]["status"] == "active"
        assert keys[0]["api_key"] is None
        assert "*" in keys[0]["key_masked"]

    @pytest.mark.asyncio
    async def test_delete_api_key_revokes_active_key(
        self, auth_app_client: AsyncClient, test_user_with_key, auth_headers, auth_db_logger
    ):
        """Test deleting an API key revokes the active key."""
        response = await auth_app_client.delete(
            f"/user/api-keys/{test_user_with_key['key_prefix']}",
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["key_prefix"] == test_user_with_key["key_prefix"]
        assert data["status"] == "revoked"

        async with auth_db_logger.pool.acquire() as conn:
            key_row = await conn.fetchrow(
                "SELECT status FROM api_keys WHERE key_prefix = $1",
                test_user_with_key["key_prefix"],
            )
        assert key_row["status"] == "revoked"

    @pytest.mark.asyncio
    async def test_delete_api_key_invalidates_auth_cache(
        self, auth_app_client: AsyncClient, test_user_with_key, auth_headers, auth_backend
    ):
        """Regression: revoking a key via DELETE must clear the auth cache.

        Before the fix, delete_api_key used db_logger.pool directly, bypassing
        CachedOperationalStore. The 30s auth cache entry remained valid after
        revocation, allowing the key to still pass authentication.
        """
        from serving.servers.auth import hash_api_key

        operational_store, _, _, _ = auth_backend
        key_hash = hash_api_key(test_user_with_key["api_key"])

        # Warm the cache with the active key's auth context
        ctx_before = await operational_store.get_auth_context_by_key_hash(key_hash)
        assert ctx_before is not None, "Key should be active and cacheable before revoke"

        # Revoke via the user self-service endpoint
        response = await auth_app_client.delete(
            f"/user/api-keys/{test_user_with_key['key_prefix']}",
            headers=auth_headers,
        )
        assert response.status_code == 200

        # Cache must be cleared: subsequent lookup should return None
        ctx_after = await operational_store.get_auth_context_by_key_hash(key_hash)
        assert ctx_after is None, "Auth cache must be invalidated after key revocation"

    @pytest.mark.asyncio
    async def test_delete_api_key_not_found(
        self, auth_app_client: AsyncClient, test_user, auth_headers
    ):
        """Test deleting an API key that does not belong to the user."""
        response = await auth_app_client.delete("/user/api-keys/hyi-missing", headers=auth_headers)

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_api_key_removes_revoked_key(
        self, auth_app_client: AsyncClient, test_user_with_key, auth_headers, auth_db_logger
    ):
        """Test deleting an already-revoked API key removes it."""
        async with auth_db_logger.pool.acquire() as conn:
            await conn.execute(
                "UPDATE api_keys SET status = 'revoked' WHERE key_prefix = $1",
                test_user_with_key["key_prefix"],
            )

        response = await auth_app_client.delete(
            f"/user/api-keys/{test_user_with_key['key_prefix']}",
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["key_prefix"] == test_user_with_key["key_prefix"]
        assert data["status"] == "deleted"

        async with auth_db_logger.pool.acquire() as conn:
            key_row = await conn.fetchrow(
                "SELECT status FROM api_keys WHERE key_prefix = $1",
                test_user_with_key["key_prefix"],
            )
        assert key_row is None

    @pytest.mark.asyncio
    async def test_regenerate_api_key_success(
        self, auth_app_client: AsyncClient, test_user_with_key, auth_headers, auth_db_logger
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

        async with auth_db_logger.pool.acquire() as conn:
            audit_row = await conn.fetchrow(
                """
                SELECT action, details, success
                FROM admin_audit_log
                WHERE target_user_id = $1 AND action = 'regenerate_key'
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                test_user_with_key["id"],
            )
        assert audit_row is not None
        assert audit_row["success"] is True
        details = audit_row["details"]
        if isinstance(details, str):
            details = json.loads(details)
        assert details["actor"] == "user"
        assert details["old_key_prefix"] == old_key_prefix
        assert details["new_key_prefix"] == data["key_prefix"]
        assert data["api_key"] not in json.dumps(details)

    @pytest.mark.asyncio
    async def test_regenerate_api_key_no_existing_key(
        self, auth_app_client: AsyncClient, test_user, auth_headers
    ):
        """Test regenerating API key when user has no key fails."""
        response = await auth_app_client.post("/user/api-keys/regenerate", headers=auth_headers)

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_regenerate_api_key_transaction(
        self, auth_app_client: AsyncClient, test_user_with_key, auth_headers, auth_backend
    ):
        """Test that regenerate uses transaction (old key revoked, new key created)."""
        operational_store, _, _, _ = auth_backend

        # Regenerate
        response = await auth_app_client.post("/user/api-keys/regenerate", headers=auth_headers)
        assert response.status_code == 200

        # Verify via store: the user should still have exactly one active key
        has_active = await operational_store.check_active_key_exists(test_user_with_key["id"])
        assert has_active, "User should have exactly one active key after regeneration"


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
        assert "remaining_today_usd" in data["quota"]
        assert "reset_at" in data["quota"]
        assert data["quota"]["reset_timezone"] == "UTC"
        # An overlay-less deployment has no support address. Site-specific
        # tests cover the configured identity separately.
        assert data["quota"]["contact_email"] == ""

    @pytest.mark.asyncio
    async def test_get_usage_with_data(
        self, auth_app_client: AsyncClient, test_user_with_key, auth_headers, auth_backend
    ):
        """Test getting usage statistics with actual usage data."""
        _, log_store, _, _ = auth_backend

        # Insert usage rows via log store
        for i in range(2):
            await log_store.log_request(
                request_id=f"req-usage-{test_user_with_key['id']}-{i}",
                model_id="test-model",
                provider="test-provider",
                prompt="test",
                response="test",
                usage={"prompt_tokens": 100 + i * 100, "completion_tokens": 50 + i * 30},
                latency_ms=100,
                status_code=200,
                metadata={"user_id": test_user_with_key["id"]},
                pricing={"prompt": "0.0001", "completion": "0.0001"},
            )

        # Flush buffered writes when the log store supports it.
        if hasattr(log_store, "flush"):
            await log_store.flush()

        response = await auth_app_client.get("/user/usage?period=all", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["usage"]["requests"] >= 2
        assert data["usage"]["cost_usd"] > 0
        # i=0: 100/50, i=1: 200/80 → totals 300 / 130
        assert data["usage"]["prompt_tokens"] >= 300
        assert data["usage"]["completion_tokens"] >= 130


class TestRecentRequests:
    """Test recent request listing endpoint."""

    @pytest.mark.asyncio
    async def test_recent_requests_filters_by_model(
        self, auth_app_client: AsyncClient, test_user_with_key, auth_headers, auth_db_logger
    ):
        """Test model_id filtering supports case-insensitive partial matching."""
        model_a = f"gpt-4o-mini-{test_user_with_key['id']}"
        model_b = f"claude-sonnet-{test_user_with_key['id']}"
        request_id_a = f"req-recent-{test_user_with_key['id']}-a"
        request_id_b = f"req-recent-{test_user_with_key['id']}-b"

        async with auth_db_logger.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO api_logs (
                    request_id, model_id, provider, user_id, status_code
                )
                VALUES
                    ($1, $2, 'test-provider', $3, 200),
                    ($4, $5, 'test-provider', $3, 200)
                """,
                request_id_a,
                model_a,
                test_user_with_key["id"],
                request_id_b,
                model_b,
            )

        response = await auth_app_client.get(
            "/user/recent-requests?model_id=4O-MINI",
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert [request["request_id"] for request in data["requests"]] == [request_id_a]
        assert data["requests"][0]["model_id"] == model_a

    @pytest.mark.asyncio
    async def test_recent_requests_surfaces_minimax_cached_tokens(
        self, auth_app_client: AsyncClient, test_user_with_key, auth_headers, auth_db_logger
    ):
        """Nested MiniMax cache usage must surface as cache_read_tokens in recent requests."""
        request_id = f"req-recent-minimax-cache-{test_user_with_key['id']}"

        await auth_db_logger.log_request(
            request_id=request_id,
            model_id="minimax-m2.7",
            provider="minimax",
            prompt=[{"role": "user", "content": "hi"}],
            response=None,
            usage={
                "prompt_tokens": 200,
                "completion_tokens": 12,
                "total_tokens": 212,
                "input_tokens_details": {"cached_tokens": 80},
            },
            latency_ms=123,
            status_code=200,
            metadata={"user_id": test_user_with_key["id"]},
        )

        response = await auth_app_client.get("/user/recent-requests", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()
        matching = [request for request in data["requests"] if request["request_id"] == request_id]
        assert len(matching) == 1
        assert matching[0]["cache_read_tokens"] == 80


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

    @pytest.mark.asyncio
    async def test_update_profile_blank_username(
        self, auth_app_client: AsyncClient, test_user, auth_headers
    ):
        """Test updating profile with a blank username fails."""
        response = await auth_app_client.patch(
            "/user/profile", headers=auth_headers, json={"user_name": "  "}
        )

        assert response.status_code == 422


class TestLLMProberLayout:
    """Test llm-prober layout preference endpoints."""

    @pytest.mark.asyncio
    async def test_get_llm_prober_layout_defaults(
        self, auth_app_client: AsyncClient, test_user, auth_headers
    ):
        response = await auth_app_client.get(
            "/user/preferences/llm-prober-layout",
            headers=auth_headers,
        )

        assert response.status_code == 200
        assert response.json() == {
            "layout": {"direct_models": [], "direct_providers": {}, "e2e_models": []}
        }

    @pytest.mark.asyncio
    async def test_update_and_reset_llm_prober_layout(
        self, auth_app_client: AsyncClient, test_user, auth_headers, auth_backend
    ):
        operational_store, _, _, _ = auth_backend
        layout = {
            "direct_models": ["glm-4.7", "glm-5", "qwen3-coder-30b"],
            "direct_providers": {
                "glm-4.7": ["glm-4.7::ollama::ollama-com", "glm-4.7::zai::api-z-ai"],
                "minimax-m2.7": ["a", "b", "c"],
            },
            "e2e_models": ["glm-4.7-flash", "minimax-m2.7"],
        }

        update_response = await auth_app_client.put(
            "/user/preferences/llm-prober-layout",
            headers=auth_headers,
            json=layout,
        )

        assert update_response.status_code == 200
        assert update_response.json() == {"layout": layout}

        fetch_response = await auth_app_client.get(
            "/user/preferences/llm-prober-layout",
            headers=auth_headers,
        )
        assert fetch_response.status_code == 200
        assert fetch_response.json() == {"layout": layout}

        # Verify preferences stored correctly via store
        prefs = await operational_store.get_user_preferences(test_user["id"])
        assert prefs.get("llm_prober_layout") == layout

        reset_response = await auth_app_client.delete(
            "/user/preferences/llm-prober-layout",
            headers=auth_headers,
        )
        assert reset_response.status_code == 200
        assert reset_response.json() == {
            "layout": {"direct_models": [], "direct_providers": {}, "e2e_models": []}
        }


class TestConcurrentAPIKeyCreation:
    """Test concurrent API key creation (database constraint)."""

    @pytest.mark.asyncio
    async def test_concurrent_key_creation_prevented(
        self, auth_app_client: AsyncClient, test_user, auth_headers, auth_backend
    ):
        """Test that concurrent key creation is prevented by database constraint.

        Note: This test assumes the unique constraint is in place.
        """
        import asyncio

        operational_store, _, _, _ = auth_backend

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
        success_count = status_codes.count(201)
        assert success_count == 1, "Only one concurrent key creation should succeed"

        # Verify one active key exists
        has_active = await operational_store.check_active_key_exists(test_user["id"])
        assert has_active, "Only one active key should exist"


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
            expires_delta=timedelta(seconds=-1),  # Already expired
        )

        headers = {"Authorization": f"Bearer {expired_token}"}
        response = await auth_app_client.get("/user/me", headers=headers)

        assert response.status_code == 401
        assert "expired" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_suspended_user_cannot_access(
        self, auth_app_client: AsyncClient, test_user, auth_headers, auth_backend
    ):
        """Test that suspended user cannot access protected endpoints."""
        operational_store, _, _, _ = auth_backend
        await operational_store.update_user_fields(test_user["id"], status="suspended")

        response = await auth_app_client.get("/user/me", headers=auth_headers)

        assert response.status_code == 403
        assert "suspended" in response.json()["detail"].lower()
