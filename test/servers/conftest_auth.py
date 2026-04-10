"""Auth-specific fixtures for testing authentication system.

Supports both PostgreSQL and Cloudflare D1 backends. The backend is selected
automatically based on DB_BACKEND in the environment (or .env).
"""

import os
import sys
from decimal import Decimal
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
from test.fixtures.auth_factories import create_test_user

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_env(monkeypatch):
    """Set up environment variables for auth testing."""
    from serving.config.settings import get_settings

    get_settings.cache_clear()

    test_env = {
        # Database
        "DB_ENABLED": "true",
        "DB_HOST": os.getenv("TEST_DB_HOST", "localhost"),
        "DB_PORT": os.getenv("TEST_DB_PORT", "5432"),
        "DB_NAME": os.getenv("TEST_DB_NAME", "freeinference_test_db"),
        "DB_USER": os.getenv("TEST_DB_USER", "postgres"),
        "DB_PASSWORD": os.getenv("TEST_DB_PASSWORD", "postgres"),
        # JWT
        "JWT_SECRET_KEY": "test-secret-key-for-testing-only-do-not-use-in-production",
        "JWT_ALGORITHM": "HS256",
        "JWT_ACCESS_TOKEN_EXPIRE_MINUTES": "15",
        "JWT_REFRESH_TOKEN_EXPIRE_DAYS": "30",
        # API Key
        "API_KEY_SECRET": "test-api-key-secret-for-testing-only",
        # Cookie
        "COOKIE_SECURE": "false",
        "COOKIE_SAMESITE": "lax",
        # Signup
        "SIGNUP_ENABLED": "1",
        "SIGNUP_DEFAULT_TIER": "free",
        "SIGNUP_DEFAULT_DAILY_QUOTA_USD": "100.00",
        "SIGNUP_REQUIRE_EMAIL_VERIFICATION": "0",
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

    get_settings.cache_clear()

    return test_env


# ---------------------------------------------------------------------------
# PostgreSQL helpers
# ---------------------------------------------------------------------------

_ALLOWED_TEST_DB_PATTERN = "_test_"


async def _guard_test_db_only(conn) -> None:
    """Fail fast unless the connection points at a dedicated test database."""
    db_name = await conn.fetchval("SELECT current_database()")
    if _ALLOWED_TEST_DB_PATTERN not in (db_name or ""):
        pytest.fail(
            f"SAFETY: refusing to run destructive operations against "
            f"database '{db_name}' (name does not contain "
            f"'{_ALLOWED_TEST_DB_PATTERN}'). "
            f"Set TEST_DB_NAME to a dedicated test database."
        )


async def _init_pg_backend():
    """Initialize PostgreSQL backend for auth tests.

    Returns (db_logger, operational_store, log_store) or raises to skip.
    """
    from serving.config.settings import get_settings
    from serving.storage.cache import CachedOperationalStore, InMemoryCache
    from serving.storage.postgres_log import PostgresLogStore
    from serving.storage.postgres_operational import PostgresOperationalStore

    test_db_name = os.getenv("TEST_DB_NAME", "freeinference_test_db")

    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(
            f"SAFETY: TEST_DB_NAME='{test_db_name}' does not contain "
            f"'{_ALLOWED_TEST_DB_PATTERN}'. Refusing to initialize."
        )

    db_config = {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }

    logger = DatabaseLogger(db_config=db_config)

    try:
        await logger.initialize()
    except Exception as e:
        pytest.skip(f"PostgreSQL not available: {e}")

    if logger.pool:
        async with logger.pool.acquire() as conn:
            await _guard_test_db_only(conn)

    settings = get_settings()
    pg_op = PostgresOperationalStore(logger.pool)
    operational_store = CachedOperationalStore(pg_op, InMemoryCache())
    log_store = PostgresLogStore(
        logger.pool,
        store_full_prompts=settings.db_store_full_content,
        use_chunked_hash=True,
    )

    return logger, operational_store, log_store


async def _cleanup_pg_tables(pool):
    """Delete all rows from auth tables in PostgreSQL."""
    async with pool.acquire() as conn:
        await _guard_test_db_only(conn)
        await conn.execute("DELETE FROM email_verification_tokens")
        await conn.execute("DELETE FROM password_reset_tokens")
        await conn.execute("DELETE FROM auth_sessions")
        await conn.execute("DELETE FROM api_keys WHERE account_id IS NOT NULL")
        await conn.execute("DELETE FROM users")


# ---------------------------------------------------------------------------
# D1 helpers
# ---------------------------------------------------------------------------


async def _init_d1_backend():
    """Initialize Cloudflare D1 backend for auth tests.

    Returns (d1_client, operational_store, log_store) or raises to skip.
    """
    from serving.config.settings import get_settings
    from serving.storage.cache import CachedOperationalStore, InMemoryCache
    from serving.storage.d1_client import D1Client
    from serving.storage.d1_log import D1LogStore
    from serving.storage.d1_operational import D1OperationalStore

    settings = get_settings()

    if not all([settings.d1_account_id, settings.d1_database_id, settings.d1_api_token]):
        pytest.skip("D1 credentials not configured")

    client = D1Client(
        account_id=settings.d1_account_id,
        database_id=settings.d1_database_id,
        api_token=settings.d1_api_token,
    )

    try:
        if not await client.health_check():
            pytest.skip("D1 not reachable")
    except Exception as e:
        await client.close()
        pytest.skip(f"D1 not available: {e}")

    d1_op = D1OperationalStore(client)
    await d1_op.initialize()

    d1_log = D1LogStore(client, flush_interval=1.0, flush_size=10)
    await d1_log.initialize()

    operational_store = CachedOperationalStore(d1_op, InMemoryCache())

    return client, operational_store, d1_log


async def _cleanup_d1_tables(client):
    """Delete all rows from auth tables in D1."""
    await client.batch(
        [
            ("DELETE FROM email_verification_tokens", None),
            ("DELETE FROM password_reset_tokens", None),
            ("DELETE FROM admin_audit_log", None),
            ("DELETE FROM auth_sessions", None),
            ("DELETE FROM user_daily_cost", None),
            ("DELETE FROM api_keys WHERE account_id IS NOT NULL", None),
            ("DELETE FROM users", None),
        ]
    )


# ---------------------------------------------------------------------------
# Backend-agnostic fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def auth_backend(auth_env):
    """Initialize the appropriate storage backend for auth tests.

    Yields (operational_store, log_store, db_logger_or_none).
    Automatically selects D1 or PostgreSQL based on DB_BACKEND.
    """
    from serving.config.settings import get_settings

    settings = get_settings()

    if settings.db_backend == "d1":
        client, operational_store, log_store = await _init_d1_backend()
        try:
            yield operational_store, log_store, None, "d1"
        finally:
            await log_store.cleanup()
            await client.close()
    else:
        logger, operational_store, log_store = await _init_pg_backend()
        try:
            yield operational_store, log_store, logger, "postgres"
        finally:
            await logger.cleanup()


@pytest_asyncio.fixture
async def clean_auth_tables(auth_backend):
    """Clean auth-related tables before and after each test."""
    operational_store, log_store, db_logger, backend = auth_backend

    if backend == "d1":
        d1_client = operational_store._store._d1  # CachedStore -> D1Store -> client
        await _cleanup_d1_tables(d1_client)
    else:
        await _cleanup_pg_tables(db_logger.pool)

    yield

    if backend == "d1":
        d1_client = operational_store._store._d1
        await _cleanup_d1_tables(d1_client)
    else:
        await _cleanup_pg_tables(db_logger.pool)


@pytest_asyncio.fixture
async def test_user(auth_backend, clean_auth_tables):
    """Create a test user in the database.

    Returns:
        dict with user data including plain text password
    """
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

    return user_data


@pytest_asyncio.fixture
async def test_user_with_key(auth_backend, test_user):
    """Create a test user with an API key.

    Returns:
        dict with user data and api_key_data
    """
    from serving.servers.auth import generate_api_key, hash_api_key

    operational_store, _, _, _ = auth_backend

    api_key = generate_api_key()
    key_hash = hash_api_key(api_key)
    key_prefix = api_key[:12]

    await operational_store.create_key(
        key_hash=key_hash,
        key_prefix=key_prefix,
        user_id=test_user["id"],
        account_id=test_user["id"],
        tier="free",
        quota_daily_cost_usd=Decimal("100.00"),
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


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def auth_app_services(auth_backend):
    """Create AppServices for auth testing.

    Wires up real store abstractions so that route handlers work
    correctly in integration tests with either backend.
    """
    operational_store, log_store, db_logger, _ = auth_backend

    mock_router = MagicMock()
    mock_rate_limiter = MagicMock()
    mock_rate_limiter.initialize = AsyncMock()
    mock_rate_limiter._persist_state = AsyncMock()

    services = AppServices(
        router=mock_router,
        db_logger=db_logger,
        operational_store=operational_store,
        log_store=log_store,
        rate_limiter=mock_rate_limiter,
        routing_manager=None,
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
    app.state.services = auth_app_services

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


# ---------------------------------------------------------------------------
# Legacy compatibility — tests that still reference auth_db_logger directly
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def auth_db_logger(auth_backend):
    """Provide the DatabaseLogger for tests that need raw PG access.

    Skips if backend is D1 (those tests need to be migrated to use stores).
    """
    _, _, db_logger, backend = auth_backend
    if backend != "postgres":
        pytest.skip("This test requires PostgreSQL (auth_db_logger)")
    yield db_logger
