from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest

from tests.fixtures.auth_factories import create_test_user

if TYPE_CHECKING:
    from collections.abc import Mapping

    from httpx import AsyncClient

ALLOWED_TEST_DB_PATTERN = "_test_"


def build_auth_test_env_defaults(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        "DB_ENABLED": "true",
        "DB_HOST": os.getenv("TEST_DB_HOST", "localhost"),
        "DB_PORT": os.getenv("TEST_DB_PORT", "5432"),
        "DB_NAME": os.getenv("TEST_DB_NAME", "hybridinference_test_db"),
        "DB_USER": os.getenv("TEST_DB_USER", "postgres"),
        "DB_PASSWORD": os.getenv("TEST_DB_PASSWORD", "postgres"),
        "JWT_SECRET_KEY": "test-secret-key-for-testing-only-do-not-use-in-production",
        "JWT_ALGORITHM": "HS256",
        "JWT_ACCESS_TOKEN_EXPIRE_MINUTES": "15",
        "JWT_REFRESH_TOKEN_EXPIRE_DAYS": "30",
        "API_KEY_SECRET": "test-api-key-secret-for-testing-only",
        "COOKIE_SECURE": "false",
        "COOKIE_SAMESITE": "lax",
        "SIGNUP_ENABLED": "1",
        "SIGNUP_DEFAULT_DAILY_QUOTA_USD": "100.00",
        "SIGNUP_REQUIRE_EMAIL_VERIFICATION": "0",
        "SMTP_HOST": "",
        "SMTP_USER": "",
        "SMTP_PASSWORD": "",
        "BASE_URL": "http://localhost:8000",
        "SITE_SUPPORT_EMAIL": "",
        "DISTRIBUTION_CONFIG_PATH": "",
        "DISTRIBUTION_CONFIG_MODE": "dark",
        "MODELS_CONFIG": "tests/fixtures/test_models.yaml",
        "ROUTING_CONFIG": "tests/fixtures/test_routing.yaml",
    }
    if overrides:
        env.update(dict(overrides))
    return env


def assert_test_db_name(db_name: str, context: str = "") -> None:
    if ALLOWED_TEST_DB_PATTERN not in (db_name or ""):
        pytest.fail(
            f"SAFETY: refusing to run tests against database '{db_name}' "
            f"(name does not contain '{ALLOWED_TEST_DB_PATTERN}')"
            f"{f' [{context}]' if context else ''}. "
            "Set TEST_DB_NAME / DB_NAME to a dedicated test database."
        )


async def assert_test_db_from_pool(pool, context: str = "") -> None:
    async with pool.acquire() as conn:
        db_name = await conn.fetchval("SELECT current_database()")
    assert_test_db_name(db_name, context)


async def cleanup_auth_tables(pool) -> None:
    async with pool.acquire() as conn:
        db_name = await conn.fetchval("SELECT current_database()")
        assert_test_db_name(db_name)
        async with conn.transaction():
            await conn.execute("DELETE FROM email_verification_tokens")
            await conn.execute("DELETE FROM password_reset_tokens")
            await conn.execute("DELETE FROM auth_sessions")
            await conn.execute("DELETE FROM api_keys WHERE account_id IS NOT NULL")
            await conn.execute("DELETE FROM signup_allowed_domains")
            await conn.execute("DELETE FROM users")


async def seed_test_user(operational_store, **overrides: Any) -> dict[str, Any]:
    user_data = create_test_user(**overrides)
    await operational_store.create_user(
        user_id=user_data["id"],
        email=user_data["email"],
        password_hash=user_data["password_hash"],
        user_name=user_data["user_name"],
        email_verified=user_data["email_verified"],
        status=user_data["status"],
    )
    return user_data


async def login_and_get_auth_headers(
    client: AsyncClient,
    *,
    email: str,
    password: str,
) -> dict[str, str]:
    response = await client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200, f"Login failed ({response.status_code}): {response.text}"
    response_data = response.json()
    assert "access_token" in response_data, (
        f"Login response missing access_token: got keys {sorted(response_data.keys())}"
    )
    access_token = response_data["access_token"]
    return {"Authorization": f"Bearer {access_token}"}
