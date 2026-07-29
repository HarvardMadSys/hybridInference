from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from tests.fixtures import auth_helpers


def test_build_auth_test_env_defaults_uses_test_db_inputs(monkeypatch):
    monkeypatch.setenv("TEST_DB_HOST", "db-host")
    monkeypatch.setenv("TEST_DB_PORT", "5544")
    monkeypatch.setenv("TEST_DB_NAME", "hybridinference_test_db")
    monkeypatch.setenv("TEST_DB_USER", "db-user")
    monkeypatch.setenv("TEST_DB_PASSWORD", "db-pass")

    result = auth_helpers.build_auth_test_env_defaults()

    assert result["DB_ENABLED"] == "true"
    assert result["DB_HOST"] == "db-host"
    assert result["DB_PORT"] == "5544"
    assert result["DB_NAME"] == "hybridinference_test_db"
    assert result["DB_USER"] == "db-user"
    assert result["DB_PASSWORD"] == "db-pass"
    assert result["API_KEY_SECRET"]
    assert result["SITE_SUPPORT_EMAIL"] == ""
    assert result["DISTRIBUTION_CONFIG_PATH"] == ""
    assert result["DISTRIBUTION_CONFIG_MODE"] == "dark"
    assert result["MODELS_CONFIG"] == "tests/fixtures/test_models.yaml"


def test_build_auth_test_env_defaults_applies_overrides():
    result = auth_helpers.build_auth_test_env_defaults(
        {
            "DB_ENABLED": "false",
            "BASE_URL": "http://test",
            "SIGNUP_DEFAULT_DAILY_QUOTA_USD": "42.00",
        }
    )

    assert result["DB_ENABLED"] == "false"
    assert result["BASE_URL"] == "http://test"
    assert result["SIGNUP_DEFAULT_DAILY_QUOTA_USD"] == "42.00"


def test_assert_test_db_name_accepts_dedicated_test_db():
    auth_helpers.assert_test_db_name("hybridinference_test_db")


def test_assert_test_db_name_rejects_non_test_db():
    with pytest.raises(pytest.fail.Exception, match="refusing to run tests"):
        auth_helpers.assert_test_db_name("hybridinference_prod")


@pytest.mark.asyncio
async def test_assert_test_db_from_pool_queries_current_database():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value="hybridinference_test_db")
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire_cm

    await auth_helpers.assert_test_db_from_pool(pool, context="unit-test")

    conn.fetchval.assert_awaited_once_with("SELECT current_database()")


@pytest.mark.asyncio
async def test_cleanup_auth_tables_deletes_expected_tables_in_order():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value="hybridinference_test_db")
    conn.execute = AsyncMock()
    transaction_cm = MagicMock()
    transaction_cm.__aenter__ = AsyncMock(return_value=None)
    transaction_cm.__aexit__ = AsyncMock(return_value=None)
    conn.transaction.return_value = transaction_cm
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire_cm

    await auth_helpers.cleanup_auth_tables(pool)

    assert conn.execute.await_args_list == [
        call("DELETE FROM email_verification_tokens"),
        call("DELETE FROM password_reset_tokens"),
        call("DELETE FROM auth_sessions"),
        call("DELETE FROM api_keys WHERE account_id IS NOT NULL"),
        call("DELETE FROM signup_allowed_domains"),
        call("DELETE FROM users"),
    ]
    conn.transaction.assert_called_once_with()
    transaction_cm.__aenter__.assert_awaited_once()
    transaction_cm.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_seed_test_user_creates_user_and_returns_factory_data(monkeypatch):
    user_data = {
        "id": "user-123",
        "email": "user@example.com",
        "password": "Secret123!",
        "password_hash": "hashed",
        "user_name": "User",
        "email_verified": True,
        "status": "active",
    }
    create_user = AsyncMock()
    operational_store = MagicMock(create_user=create_user)
    factory = MagicMock(return_value=user_data)
    monkeypatch.setattr(auth_helpers, "create_test_user", factory)

    result = await auth_helpers.seed_test_user(operational_store, email="override@example.com")

    assert result is user_data
    factory.assert_called_once_with(email="override@example.com")
    create_user.assert_awaited_once_with(
        user_id="user-123",
        email="user@example.com",
        password_hash="hashed",
        user_name="User",
        email_verified=True,
        status="active",
    )


@pytest.mark.asyncio
async def test_login_and_get_auth_headers_posts_login_and_returns_bearer_header():
    response = MagicMock(status_code=200)
    response.json.return_value = {"access_token": "token-123"}
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    result = await auth_helpers.login_and_get_auth_headers(
        client,
        email="user@example.com",
        password="Secret123!",
    )

    client.post.assert_awaited_once_with(
        "/auth/login",
        json={"email": "user@example.com", "password": "Secret123!"},
    )
    assert result == {"Authorization": "Bearer token-123"}


@pytest.mark.asyncio
async def test_login_and_get_auth_headers_fails_clearly_when_access_token_missing():
    response = MagicMock(status_code=200)
    response.json.return_value = {"refresh_token": "token-123"}
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    with pytest.raises(AssertionError, match="access_token"):
        await auth_helpers.login_and_get_auth_headers(
            client,
            email="user@example.com",
            password="Secret123!",
        )
