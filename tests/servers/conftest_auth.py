"""Auth-specific fixtures for testing authentication system."""

import os
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to Python path
project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient

from serving.servers.deps import AppServices
from serving.storage.database import DatabaseLogger
from tests.fixtures.auth_helpers import (
    assert_test_db_from_pool,
    assert_test_db_name,
    build_auth_test_env_defaults,
    cleanup_auth_tables,
    login_and_get_auth_headers,
    seed_test_user,
)

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_env(monkeypatch):
    """Set up environment variables for auth testing."""
    from serving.config.settings import get_settings

    get_settings.cache_clear()

    test_env = build_auth_test_env_defaults(
        {
            "MODELS_CONFIG": "tests/fixtures/test_models.yaml",
            "ROUTING_CONFIG": "tests/fixtures/test_routing.yaml",
        }
    )
    for key, value in test_env.items():
        monkeypatch.setenv(key, value)

    get_settings.cache_clear()

    return test_env


async def _init_pg_backend():
    """Initialize PostgreSQL backend for auth tests.

    Returns (db_logger, operational_store, log_store) or raises to skip.
    """
    from serving.config.settings import get_settings
    from serving.storage.cache import CachedOperationalStore, InMemoryCache
    from serving.storage.postgres_log import PostgresLogStore
    from serving.storage.postgres_operational import PostgresOperationalStore

    test_db_name = os.getenv("TEST_DB_NAME", "freeinference_test_db")
    assert_test_db_name(test_db_name, context="auth_backend init")

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
        await assert_test_db_from_pool(logger.pool, context="auth_backend pool")

    settings = get_settings()
    pg_op = PostgresOperationalStore(logger.pool)
    operational_store = CachedOperationalStore(pg_op, InMemoryCache())
    log_store = PostgresLogStore(
        logger.pool,
        store_full_prompts=settings.db_store_full_content,
    )

    return logger, operational_store, log_store


# ---------------------------------------------------------------------------
# PostgreSQL-backed fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def auth_backend(auth_env):
    """Initialize PostgreSQL storage for auth tests.

    Yields (operational_store, log_store, db_logger_or_none).
    """
    logger, operational_store, log_store = await _init_pg_backend()
    try:
        yield operational_store, log_store, logger, "postgres"
    finally:
        await logger.cleanup()


@pytest_asyncio.fixture
async def clean_auth_tables(auth_backend):
    """Clean auth-related tables before and after each test."""
    _operational_store, _log_store, db_logger, _backend = auth_backend

    await cleanup_auth_tables(db_logger.pool)
    yield

    await cleanup_auth_tables(db_logger.pool)


@pytest_asyncio.fixture
async def test_user(auth_backend, clean_auth_tables):
    """Create a test user in the database.

    Returns:
        dict with user data including plain text password
    """
    operational_store, _, _, _ = auth_backend
    return await seed_test_user(operational_store)


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
    return await login_and_get_auth_headers(
        auth_app_client,
        email=test_user["email"],
        password=test_user["password"],
    )


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

    services = AppServices(
        router=mock_router,
        db_logger=db_logger,
        operational_store=operational_store,
        log_store=log_store,
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

    This fixture is for tests that still need raw PostgreSQL access.
    """
    _, _, db_logger, backend = auth_backend
    if backend != "postgres":
        pytest.skip("This test requires PostgreSQL (auth_db_logger)")
    yield db_logger
