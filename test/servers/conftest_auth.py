"""Auth-specific fixtures for testing authentication system."""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root to Python path
project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient

from serving.servers.deps import AppServices
from serving.storage.database import DatabaseLogger
from test.fixtures.auth_factories import (
    create_test_user,
)


@pytest.fixture
def auth_env(monkeypatch):
    """Set up environment variables for auth testing."""
    # CRITICAL: Clear settings cache before setting test env vars
    # This ensures get_settings() will pick up the new environment variables
    from serving.config.settings import get_settings

    get_settings.cache_clear()

    test_env = {
        # Database
        "DB_ENABLED": "true",
        "DB_HOST": os.getenv("TEST_DB_HOST", "localhost"),
        "DB_PORT": os.getenv("TEST_DB_PORT", "5432"),
        "DB_NAME": os.getenv("TEST_DB_NAME", "freeinference_test_db"),
        "DB_USER": os.getenv("TEST_DB_USER", "postgres"),
        "DB_PASSWORD": os.getenv(
            "TEST_DB_PASSWORD", "postgres"
        ),  # Default to 'postgres' for Docker
        # JWT
        "JWT_SECRET_KEY": "test-secret-key-for-testing-only-do-not-use-in-production",
        "JWT_ALGORITHM": "HS256",
        "JWT_ACCESS_TOKEN_EXPIRE_MINUTES": "15",
        "JWT_REFRESH_TOKEN_EXPIRE_DAYS": "30",
        # API Key
        "API_KEY_SECRET": "test-api-key-secret-for-testing-only",
        # Cookie
        "COOKIE_SECURE": "false",  # Allow http in tests
        "COOKIE_SAMESITE": "lax",
        # Signup
        "SIGNUP_ENABLED": "1",
        "SIGNUP_DEFAULT_TIER": "free",
        "SIGNUP_DEFAULT_DAILY_QUOTA_USD": "100.00",
        "SIGNUP_REQUIRE_EMAIL_VERIFICATION": "0",  # Disable for easier testing
        # Email (disabled in tests)
        "SMTP_HOST": "",
        "SMTP_USER": "",
        "SMTP_PASSWORD": "",
        # Base URL
        "BASE_URL": "http://localhost:8000",
        # Disable other features
        "RATE_LIMIT_ENABLED": "0",
        "MODELS_CONFIG": "test/fixtures/test_models.yaml",
        "ROUTING_CONFIG": "test/fixtures/test_routing.yaml",
    }

    for key, value in test_env.items():
        monkeypatch.setenv(key, value)

    # Clear cache again after setting env vars to force reload
    get_settings.cache_clear()

    return test_env


@pytest_asyncio.fixture
async def auth_db_logger(auth_env):
    """Create a real DatabaseLogger for auth testing.

    Note: This requires a test database to be set up.
    Set TEST_DB_NAME env var to use a different database.
    """
    db_config = {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": os.getenv("TEST_DB_NAME", "freeinference_test_db"),
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),  # Default to 'postgres' for Docker
    }

    logger = DatabaseLogger(db_config=db_config)

    try:
        await logger.initialize()
    except Exception as e:
        pytest.skip(f"PostgreSQL not available: {e}")

    yield logger

    # Cleanup
    await logger.cleanup()


@pytest_asyncio.fixture
async def clean_auth_tables(auth_db_logger):
    """Clean auth-related tables before each test."""
    async with auth_db_logger.pool.acquire() as conn:
        # Delete in reverse order of dependencies
        await conn.execute("DELETE FROM email_verification_tokens")
        await conn.execute("DELETE FROM password_reset_tokens")
        await conn.execute("DELETE FROM auth_sessions")
        await conn.execute(
            "DELETE FROM api_keys WHERE account_id IS NOT NULL"
        )  # Only self-registered keys
        await conn.execute("DELETE FROM users")

    yield

    # Cleanup after test
    async with auth_db_logger.pool.acquire() as conn:
        await conn.execute("DELETE FROM email_verification_tokens")
        await conn.execute("DELETE FROM password_reset_tokens")
        await conn.execute("DELETE FROM auth_sessions")
        await conn.execute("DELETE FROM api_keys WHERE account_id IS NOT NULL")
        await conn.execute("DELETE FROM users")


@pytest_asyncio.fixture
async def test_user(auth_db_logger, clean_auth_tables):
    """Create a test user in the database.

    Returns:
        dict with user data including plain text password
    """
    user_data = create_test_user()

    async with (
        auth_db_logger.pool.acquire() as conn,
        conn.transaction(),
    ):
        await conn.execute(
            """
            INSERT INTO users (id, email, password_hash, user_name, status, email_verified)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            user_data["id"],
            user_data["email"].lower(),  # Store email in lowercase
            user_data["password_hash"],
            user_data["user_name"],
            user_data["status"],
            user_data["email_verified"],
        )

    return user_data


@pytest_asyncio.fixture
async def test_user_with_key(auth_db_logger, test_user):
    """Create a test user with an API key.

    Returns:
        dict with user data and api_key_data
    """
    from serving.servers.auth import generate_api_key, hash_api_key

    # Generate API key
    api_key = generate_api_key()
    key_hash = hash_api_key(api_key)
    key_prefix = api_key[:12]

    async with auth_db_logger.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO api_keys (
                key_hash, key_prefix, user_id, account_id,
                status, quota_daily_cost_usd, tier
            )
            VALUES ($1, $2, $3, $4, 'active', 100.00, 'free')
            """,
            key_hash,
            key_prefix,
            test_user["id"],
            test_user["id"],
        )

    test_user["api_key"] = api_key
    test_user["key_prefix"] = key_prefix

    return test_user


@pytest_asyncio.fixture
async def auth_headers(test_user, auth_app_client):
    """Get authentication headers for a test user.

    Returns:
        dict with Authorization header
    """
    # Login to get access token
    response = await auth_app_client.post(
        "/auth/login",
        json={
            "email": test_user["email"],
            "password": test_user["password"],
        },
    )

    assert response.status_code == 200
    data = response.json()
    access_token = data["access_token"]

    return {"Authorization": f"Bearer {access_token}"}


@pytest_asyncio.fixture
async def auth_app_services(auth_db_logger):
    """Create AppServices for auth testing."""
    # Mock router and rate limiter (not needed for auth tests)
    mock_router = MagicMock()
    mock_rate_limiter = MagicMock()
    mock_rate_limiter.initialize = AsyncMock()
    mock_rate_limiter._persist_state = AsyncMock()

    services = AppServices(
        router=mock_router,
        db_logger=auth_db_logger,
        rate_limiter=mock_rate_limiter,
    )

    yield services


@pytest_asyncio.fixture
async def auth_test_app(auth_app_services):
    """Create a FastAPI app with auth routes for testing."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = auth_app_services
        yield

    app = FastAPI(title="Auth Test API", version="1.0.0", lifespan=lifespan)

    # Manually set services for testing (lifespan may not trigger in test client)
    app.state.services = auth_app_services

    # Import and register auth routes
    from serving.servers.routers import auth_routes, user_routes

    app.include_router(auth_routes.router)
    app.include_router(user_routes.router)

    return app


@pytest_asyncio.fixture
async def auth_app_client(auth_test_app):
    """Create an async test client for auth endpoints."""
    from httpx import ASGITransport

    transport = ASGITransport(app=auth_test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def mock_email_service(monkeypatch):
    """Mock email sending for tests."""
    mock_send = MagicMock(return_value=True)

    with patch("serving.utils.email.send_email", mock_send):
        yield mock_send
