"""Shared fixtures for server tests."""

import asyncio
import contextlib
import os
import sys
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add project root to Python path
project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient

from routing.executor import RouteExecutor
from serving.servers.deps import AppServices
from serving.servers.rate_limiter import PersistentRateLimiter
from serving.storage.database import DatabaseLogger

# ============================================================================
# Session-level fixtures
# ============================================================================


@pytest.fixture(scope="session")
def event_loop() -> Generator:
    """Create an event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


_ALLOWED_TEST_DB_PATTERN = "_test_"


def _assert_test_db_name(db_name: str, context: str = "") -> None:
    """Fail if *db_name* does not look like a dedicated test database.

    Allowlist approach: the database name must contain '_test_' (e.g.
    ``freeinference_test_db``).  This catches production, staging, copies,
    and any other non-test database.
    """
    if _ALLOWED_TEST_DB_PATTERN not in (db_name or ""):
        pytest.fail(
            f"SAFETY: refusing to run tests against database '{db_name}' "
            f"(name does not contain '{_ALLOWED_TEST_DB_PATTERN}')"
            f"{f' [{context}]' if context else ''}. "
            f"Set TEST_DB_NAME / DB_NAME to a dedicated test database."
        )


async def _assert_test_db_from_pool(pool, context: str = "") -> None:
    """Query the connection pool and verify it points at a test database."""
    async with pool.acquire() as conn:
        db_name = await conn.fetchval("SELECT current_database()")
    _assert_test_db_name(db_name, context)


async def _skip_if_test_db_unavailable(context: str = "") -> None:
    """Skip DB-backed auth tests before full app startup starts retrying."""
    import asyncpg

    db_config = {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "database": os.environ.get("DB_NAME", "freeinference_test_db"),
        "user": os.environ.get("DB_USER", "postgres"),
        "password": os.environ.get("DB_PASSWORD", "postgres"),
    }

    try:
        conn = await asyncpg.connect(**db_config, timeout=1)
    except Exception as exc:
        pytest.skip(
            f"PostgreSQL test database not available{f' [{context}]' if context else ''}: {exc}"
        )

    try:
        db_name = await conn.fetchval("SELECT current_database()")
        _assert_test_db_name(db_name, context)
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
def auth_test_env():
    """Force-set environment variables so tests never hit production.

    Uses direct assignment (NOT setdefault) to guarantee test values
    override any .env / inherited env regardless of load order.
    Original values are restored when the session ends.
    """
    _TEST_DB_VARS = {
        "DB_HOST": "localhost",
        "DB_PORT": "5432",
        "DB_NAME": "freeinference_test_db",
        "DB_USER": "postgres",
        "DB_PASSWORD": "postgres",
        # Mirror for fixtures that read TEST_DB_* directly
        "TEST_DB_HOST": "localhost",
        "TEST_DB_PORT": "5432",
        "TEST_DB_NAME": "freeinference_test_db",
        "TEST_DB_USER": "postgres",
        "TEST_DB_PASSWORD": "postgres",
    }
    _AUTH_VARS = {
        "JWT_SECRET_KEY": "test-secret-key-32-chars-long!!",
        "API_KEY_SECRET": "test-api-key-secret",
        "ADMIN_TOKEN": "test-admin-token",
        "BASE_URL": "http://test",
        "COOKIE_SECURE": "0",
        "SIGNUP_ENABLED": "1",
        "SIGNUP_DEFAULT_DAILY_QUOTA_USD": "10.00",
        # Disabled by default for backward compatibility with existing tests
        "SIGNUP_REQUIRE_EMAIL_VERIFICATION": "0",
        # Turnstile disabled by default; per-test setenv to enable verification.
        "TURNSTILE_SECRET_KEY": "",
        # Disable SMTP in tests to avoid sending real emails
        "SMTP_HOST": "",
        "SMTP_USER": "",
        "SMTP_PASSWORD": "",
    }
    _SERVICE_VARS = {
        "DB_ENABLED": "true",
        "MODELS_CONFIG": "test/fixtures/test_models.yaml",
        "ROUTING_CONFIG": "test/fixtures/test_routing.yaml",
        "RATE_LIMIT_ENABLED": "0",
        "METRICS_ENABLED": "0",
        "OFFLOAD": "0",
    }

    all_vars = {**_TEST_DB_VARS, **_AUTH_VARS, **_SERVICE_VARS}
    saved = {k: os.environ.get(k) for k in all_vars}

    for key, value in all_vars.items():
        os.environ[key] = value

    yield

    # Restore original environment
    for key, original in saved.items():
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original


# ============================================================================
# Per-test signup rate-limit reset (prevents bleed across tests since
# ASGITransport gives every request the same default client host).
# ============================================================================


@pytest.fixture(autouse=True)
def _reset_signup_rate_limit():
    from serving.utils.signup_rate_limit import reset_signup_rate_limit_state

    reset_signup_rate_limit_state()
    yield


# ============================================================================
# Mock fixtures (for non-auth tests)
# ============================================================================


@pytest.fixture
def mock_env(monkeypatch):
    """Mock environment variables for testing."""
    test_env = {
        "DB_ENABLED": "false",  # Disable DB in tests by default
        "RATE_LIMIT_ENABLED": "0",  # Disable rate limiting in tests
        "MODELS_CONFIG": "test/fixtures/test_models.yaml",
        "ROUTING_CONFIG": "test/fixtures/test_routing.yaml",
        "LOCAL_BASE_URL": "http://localhost:8001",
        "OFFLOAD": "0",
    }
    for key, value in test_env.items():
        monkeypatch.setenv(key, value)
    return test_env


@pytest.fixture
def mock_router():
    """Create a mock RouteExecutor with test routes."""
    router = RouteExecutor()

    # Create mock adapter
    mock_adapter = MagicMock()
    mock_adapter.config.provider = "test"
    mock_adapter.config.base_url = "http://test.local"
    mock_adapter.config.context_length = 8192
    mock_adapter.config.max_output_length = 4096
    mock_adapter.config.supported_params = ["temperature", "max_tokens"]
    mock_adapter.config.supports_tools = False
    mock_adapter.config.supports_structured_output = False
    mock_adapter.config.id = "test-model"
    mock_adapter.config.name = "Test Model"
    mock_adapter.config.quantization = "bf16"
    mock_adapter.config.input_modalities = ["text"]
    mock_adapter.config.output_modalities = ["text"]
    mock_adapter.config.pricing = {"prompt": "0", "completion": "0"}

    # Register test route
    router.register_route("test-model", [(mock_adapter, 1.0)])

    return router


@pytest.fixture
def mock_db_logger():
    """Create a mock database logger."""
    logger = MagicMock(spec=DatabaseLogger)
    logger.initialize = AsyncMock()
    logger.cleanup = AsyncMock()
    logger.log_request = AsyncMock()
    logger.get_stats = AsyncMock(return_value=[])

    # Mock the pool and connection context managers
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_acquire = MagicMock()

    # Setup the context manager chain: pool.acquire().__aenter__()
    mock_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_acquire.__aexit__ = AsyncMock(return_value=None)
    mock_pool.acquire = MagicMock(return_value=mock_acquire)

    # Attach pool to logger
    logger.pool = mock_pool

    return logger


@pytest.fixture
def mock_rate_limiter():
    """Create a mock rate limiter."""
    limiter = MagicMock(spec=PersistentRateLimiter)
    limiter.initialize = AsyncMock()
    limiter._persist_state = AsyncMock()
    limiter.acquire_tokens = AsyncMock(return_value=(True, {}))
    limiter.release_tokens = AsyncMock()
    limiter.get_status = MagicMock(
        return_value={
            "configured": True,
            "capacity": 1000000,
            "tokens_available": 1000000,
            "window_seconds": 60,
        }
    )
    limiter.get_metrics = MagicMock(return_value={})
    limiter.reset_circuit_breaker = MagicMock()
    return limiter


@pytest_asyncio.fixture
async def app_services(mock_router, mock_db_logger, mock_rate_limiter):
    """Create AppServices instance for testing."""
    services = AppServices(
        router=mock_router,
        db_logger=mock_db_logger,
        rate_limiter=mock_rate_limiter,
        routing_manager=None,
    )
    yield services

    # Cleanup
    if services.db_logger:
        with contextlib.suppress(Exception):
            # Suppress teardown errors to avoid masking test results
            await services.db_logger.cleanup()
    if services.rate_limiter:
        with contextlib.suppress(Exception):
            # Suppress teardown errors to avoid masking test results
            await services.rate_limiter._persist_state()


@pytest_asyncio.fixture
async def test_app(app_services):
    """Create a FastAPI app instance for testing."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = app_services
        yield
        # Cleanup handled by app_services fixture

    app = FastAPI(title="Test API Server", version="2.0.0", lifespan=lifespan)

    # Manually set services for testing (lifespan may not trigger in test client)
    app.state.services = app_services

    # Import and register routes from new modular structure
    from serving.servers.routers import auth_routes, health, models, user_routes

    # Include routers (they have their own routes defined)
    app.include_router(health.router)
    app.include_router(models.router)
    app.include_router(auth_routes.router)
    app.include_router(user_routes.router)

    return app


@pytest_asyncio.fixture
async def test_client(test_app):
    """Create an async test client."""
    from httpx import ASGITransport

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ============================================================================
# Auth-specific fixtures (use real app with lifespan)
# ============================================================================


@pytest_asyncio.fixture
async def auth_app(auth_test_env):
    """App instance with lifespan context for auth tests.

    This fixture creates a fresh app instance and manages its lifespan,
    ensuring app.state.services is properly initialized.

    Depends on auth_test_env to ensure test environment variables are set
    and settings cache is cleared.

    Returns:
        FastAPI: App instance with initialized services in app.state.services
    """
    # Layer 2a: pre-flight check BEFORE create_app() / lifespan can run
    # DB init (CREATE TABLE, ALTER, admin seed) to prevent schema side-effects.
    _assert_test_db_name(os.environ.get("DB_NAME", ""), context="auth_app pre-flight DB_NAME")
    await _skip_if_test_db_unavailable(context="auth_app pre-flight connection")

    # Clear settings cache to pick up test environment variables
    from serving.config.settings import get_settings
    from serving.servers.app import create_app

    get_settings.cache_clear()

    app = create_app()

    # Manually trigger lifespan startup
    async with app.router.lifespan_context(app):
        # Layer 2b: post-startup verification against the live connection
        # in case settings/dotenv overrode the env var during bootstrap.
        db_logger = getattr(getattr(app.state, "services", None), "db_logger", None)
        if db_logger and getattr(db_logger, "pool", None):
            await _assert_test_db_from_pool(db_logger.pool, context="auth_app fixture")
        yield app


@pytest_asyncio.fixture
async def auth_client(auth_app):
    """Async HTTP client with FastAPI lifespan enabled for auth tests.

    CRITICAL: Uses AsyncClient with app parameter to ensure
    app.state.services is initialized before tests run.

    This client uses the REAL app with real database and services,
    unlike test_client which uses mocks.

    Usage:
        async def test_login(auth_client):
            response = await auth_client.post("/auth/login", json={...})
            assert response.status_code == 200
    """
    from httpx import ASGITransport, AsyncClient

    # Use the app from auth_app fixture (lifespan already managed)
    transport = ASGITransport(app=auth_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# HTTP-style auth fixtures; use these for tests that should exercise the full
# app lifespan and auth endpoints instead of DB-direct seeding from conftest_auth.py.
@pytest_asyncio.fixture(name="auth_app_db_logger")
async def auth_app_db_logger_fixture(auth_app):
    """Database logger from app state (initialized in lifespan).

    CRITICAL: Access via app.state.services, not Depends(get_db_logger).
    Depends() functions cannot be called directly in tests.

    Note: This fixture depends on 'auth_app' fixture to ensure lifespan
    has run and app.state.services is populated.

    Usage:
        async def test_example(auth_client, auth_app_db_logger):
            async with auth_app_db_logger.pool.acquire() as conn:
                result = await conn.fetchrow("SELECT 1")
    """
    # CRITICAL: Access from app.state.services, not by calling get_db_logger()
    return auth_app.state.services.db_logger  # type: ignore[attr-defined]


@pytest_asyncio.fixture
async def require_db(auth_app_db_logger):
    """Skip tests that require database if not available.

    This fixture is mainly for local development where developers might not
    have PostgreSQL running. In CI, the database is always available via
    the postgres service container (see .github/workflows/ci.yml).

    Usage:
        async def test_user_creation(auth_client, require_db):
            # This test will be skipped if database is not available locally
            # In CI, it will always run since postgres service is configured
            ...
    """
    if (
        auth_app_db_logger is None
        or not hasattr(auth_app_db_logger, "pool")
        or auth_app_db_logger.pool is None
    ):
        pytest.skip("Database not available (start PostgreSQL or check DB config)")
    return auth_app_db_logger


@pytest_asyncio.fixture(name="auth_client_test_user")
async def auth_client_test_user_fixture(auth_client):
    """Create a test user for auth tests."""
    user_data = {
        "email": f"test_{os.urandom(4).hex()}@signuptest.dev",
        "password": "TestPass123!",
        "user_name": "Test User",
    }

    response = await auth_client.post("/auth/signup", json=user_data)
    assert response.status_code == 201

    user_id = response.json()["user_id"]

    return {
        **user_data,
        "user_id": user_id,
        "id": user_id,  # Add 'id' field for compatibility with conftest_auth tests
        "status": "active",  # Default status for new users
        "email_verified": False,  # Email verification is disabled in tests by default
    }


@pytest_asyncio.fixture(name="auth_client_authenticated_user")
async def auth_client_authenticated_user_fixture(auth_client, auth_client_test_user):
    """Create and authenticate a test user."""
    login_response = await auth_client.post(
        "/auth/login",
        json={
            "email": auth_client_test_user["email"],
            "password": auth_client_test_user["password"],
        },
    )

    assert login_response.status_code == 200

    return {**auth_client_test_user, "access_token": login_response.json()["access_token"]}


# ============================================================================
# Utility fixtures
# ============================================================================


@pytest.fixture
def temp_models_yaml(tmp_path):
    """Create a temporary models.yaml for testing."""
    models_yaml = tmp_path / "test_models.yaml"
    content = """
models:
  - id: test-model-1
    name: Test Model 1
    provider: vllm
    base_url: ${LOCAL_BASE_URL}
    context_length: 8192
    max_output_length: 4096
    aliases: ["test-alias-1"]
    route:
      - kind: vllm
        weight: 1.0
        base_url: ${LOCAL_BASE_URL}

  - id: test-model-2
    name: Test Model 2
    provider: zhipu
    base_url: http://remote.test
    api_key: test-key
    context_length: 16384
    max_output_length: 8192
    aliases: ["test-alias-2"]
    route:
      - kind: zhipu
        weight: 1.0
        base_url: http://remote.test
        api_key: test-key
"""
    models_yaml.write_text(content)
    return str(models_yaml)


@pytest.fixture
def temp_routing_yaml(tmp_path):
    """Create a temporary routing.yaml for testing."""
    routing_yaml = tmp_path / "test_routing.yaml"
    content = """
routing_strategy: fixed
routing_parameter:
  local_fraction: 0.7
timeout: 2
health_check: 0
local_deployment:
  - endpoint: ${LOCAL_BASE_URL:-http://localhost:8001}
    models:
      - test-model-1
remote_deployment:
  - endpoint: http://remote.test
    models:
      - test-model-2
"""
    routing_yaml.write_text(content)
    return str(routing_yaml)


# Performance test skip marker
skip_if_not_perf = pytest.mark.skipif(
    os.getenv("RUN_PERF") != "1",
    reason="Performance tests are disabled by default (set RUN_PERF=1 to enable)",
)
