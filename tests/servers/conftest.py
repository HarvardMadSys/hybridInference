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
from serving.storage.base import LogStore, OperationalStore
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


def _xdist_worker_id() -> str:
    """Return the current xdist worker id, or 'master' when running serially.

    Read from the env var (set by xdist before any fixture runs) rather than
    the `worker_id` fixture so this can be called from session-scoped autouse
    fixtures without scope/ordering issues.
    """
    return os.environ.get("PYTEST_XDIST_WORKER", "master")


def _worker_db_name(base: str, worker_id: str) -> str:
    """Suffix the base test DB name with the xdist worker id.

    When running serially (worker_id == 'master') the base name is returned
    unchanged so existing setups keep working.
    """
    if worker_id == "master":
        return base
    return f"{base}_{worker_id}"


def _ensure_worker_database(db_name: str) -> None:
    """Create the worker-specific PostgreSQL database if it doesn't exist.

    Connects to the `postgres` maintenance DB to issue CREATE DATABASE.
    Idempotent: checks pg_database first, since CREATE DATABASE has no
    `IF NOT EXISTS` clause.
    """
    import asyncpg

    host = os.environ.get("TEST_DB_HOST", "localhost")
    port = int(os.environ.get("TEST_DB_PORT", "5432"))
    user = os.environ.get("TEST_DB_USER", "postgres")
    password = os.environ.get("TEST_DB_PASSWORD", "postgres")

    async def _create() -> None:
        conn = await asyncpg.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database="postgres",
            timeout=5,
        )
        try:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)
            if not exists:
                # Database identifier cannot be parameterized; safe because
                # db_name is derived from controlled env vars + xdist worker id.
                await conn.execute(f'CREATE DATABASE "{db_name}"')
        finally:
            await conn.close()

    # If postgres isn't reachable here, the existing skip logic in
    # _skip_if_test_db_unavailable will handle it on a per-test basis.
    with contextlib.suppress(Exception):
        asyncio.run(_create())


@pytest.fixture(scope="session", autouse=True)
def auth_test_env():
    """Force-set environment variables so tests never hit production.

    Uses direct assignment (NOT setdefault) to guarantee test values
    override any .env / inherited env regardless of load order.
    Original values are restored when the session ends.
    """
    # Respect TEST_DB_* env vars from the process (e.g. command-line overrides),
    # falling back to Docker-friendly defaults.
    _db_host = os.environ.get("TEST_DB_HOST", "localhost")
    _db_port = os.environ.get("TEST_DB_PORT", "5432")
    _base_db_name = os.environ.get("TEST_DB_NAME", "freeinference_test_db")
    _worker_id = _xdist_worker_id()
    _db_name = _worker_db_name(_base_db_name, _worker_id)
    if _worker_id != "master":
        # Only need to provision a per-worker DB under xdist. The base DB
        # is provisioned by the postgres service container in CI / by the
        # developer locally.
        _ensure_worker_database(_db_name)
    _db_user = os.environ.get("TEST_DB_USER", "postgres")
    _db_pass = os.environ.get("TEST_DB_PASSWORD", "postgres")
    _TEST_DB_VARS = {
        "DB_HOST": _db_host,
        "DB_PORT": _db_port,
        "DB_NAME": _db_name,
        "DB_USER": _db_user,
        "DB_PASSWORD": _db_pass,
        # Mirror for fixtures that read TEST_DB_* directly
        "TEST_DB_HOST": _db_host,
        "TEST_DB_PORT": _db_port,
        "TEST_DB_NAME": _db_name,
        "TEST_DB_USER": _db_user,
        "TEST_DB_PASSWORD": _db_pass,
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
        "METRICS_ENABLED": "0",
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


@pytest.fixture(autouse=True)
def _reset_login_rate_limit():
    from serving.utils.login_rate_limit import reset_login_rate_limit_state

    reset_login_rate_limit_state()
    yield


# ============================================================================
# Mock fixtures (for non-auth tests)
# ============================================================================


@pytest.fixture
def mock_env(monkeypatch):
    """Mock environment variables for testing."""
    test_env = {
        "DB_ENABLED": "false",  # Disable DB in tests by default
        "MODELS_CONFIG": "test/fixtures/test_models.yaml",
        "ROUTING_CONFIG": "test/fixtures/test_routing.yaml",
        "LOCAL_BASE_URL": "http://localhost:8001",
    }
    for key, value in test_env.items():
        monkeypatch.setenv(key, value)
    # Clear cached settings so bootstrap reads the test env
    from serving.config.settings import get_settings

    get_settings.cache_clear()
    yield test_env
    get_settings.cache_clear()


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
def mock_operational_store():
    """Create a mock operational store with all methods as AsyncMock."""
    store = MagicMock(spec=OperationalStore)
    for attr_name in dir(OperationalStore):
        if not attr_name.startswith("_"):
            method = getattr(OperationalStore, attr_name)
            if callable(method):
                setattr(store, attr_name, AsyncMock())
    return store


@pytest.fixture
def mock_log_store():
    """Create a mock log store with all methods as AsyncMock."""
    store = MagicMock(spec=LogStore)
    for attr_name in dir(LogStore):
        if not attr_name.startswith("_"):
            method = getattr(LogStore, attr_name)
            if callable(method):
                setattr(store, attr_name, AsyncMock())
    # Sensible defaults
    store.get_user_cost_today = AsyncMock(return_value=0.0)
    store.get_user_cost_period = AsyncMock(return_value=0.0)
    store.get_batch_usage = AsyncMock(return_value={})
    store.get_user_usage_detail = AsyncMock(
        return_value={
            "today": {"cost_usd": 0.0, "requests": 0},
            "week": {"cost_usd": 0.0, "requests": 0},
            "month": {"cost_usd": 0.0, "requests": 0},
            "alltime": {"cost_usd": 0.0, "requests": 0},
        }
    )
    store.log_request = AsyncMock()
    return store


@pytest_asyncio.fixture
async def app_services(mock_router, mock_db_logger, mock_operational_store, mock_log_store):
    """Create AppServices instance for testing."""
    services = AppServices(
        router=mock_router,
        db_logger=mock_db_logger,
        operational_store=mock_operational_store,
        log_store=mock_log_store,
        routing_manager=None,
    )
    yield services

    # Cleanup
    if services.db_logger:
        with contextlib.suppress(Exception):
            # Suppress teardown errors to avoid masking test results
            await services.db_logger.cleanup()


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
async def require_db(auth_app):
    """Skip tests that require database if not available.

    Returns the operational store from the app services.

    Usage:
        async def test_user_creation(auth_client, require_db):
            # require_db is the operational store
            await require_db.update_user_fields(user_id, email_verified=True)
    """
    services = getattr(auth_app.state, "services", None)
    op_store = getattr(services, "operational_store", None) if services else None
    if op_store is None:
        pytest.skip("Database not available (start PostgreSQL or check DB config)")
    return op_store


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


# ============================================================================
# Anthropic compat router fixtures
# ============================================================================

ANTHROPIC_TEST_API_KEY = "hyi-anthropic-compat-test"


@pytest_asyncio.fixture
async def anthropic_compat_router():
    """RouteExecutor with one anthropic-kind and one zhipu-kind real adapter."""
    from routing.executor import RouteExecutor
    from serving.adapters import AnthropicAdapter, OpenAICompatAdapter
    from serving.adapters.base import ModelConfig

    re = RouteExecutor()

    anthropic_cfg = ModelConfig(
        id="claude-opus-4.7",
        name="Claude Opus 4.7",
        provider="anthropic",
        base_url="https://api.anthropic.com",
        api_key="sk-ant-test",
        provider_model_id="claude-opus-4-7",
        max_output_length=1024,
        supports_tools=True,
        supported_params=["temperature", "max_tokens", "top_p", "stream", "tools", "tool_choice"],
        pricing={
            "prompt": "5.0",
            "completion": "25.0",
            "image": "0",
            "request": "0",
            "input_cache_reads": "0.5",
            "input_cache_writes": "6.25",
        },
    )
    zhipu_cfg = ModelConfig(
        id="glm-4.7",
        name="GLM-4.7",
        provider="zhipu",
        base_url="https://example-zhipu.test",
        api_key="zhipu-test",
        chat_path="/chat/completions",
        max_output_length=1024,
        supports_tools=True,
        supported_params=["temperature", "max_tokens", "stop", "stream", "tools", "tool_choice"],
        pricing={
            "prompt": "0.6",
            "completion": "2.2",
            "image": "0",
            "request": "0",
            "input_cache_reads": "0.11",
            "input_cache_writes": "0",
        },
    )
    re.register_route("claude-opus-4.7", [(AnthropicAdapter(anthropic_cfg), 1.0)])
    re.register_route("glm-4.7", [(OpenAICompatAdapter(zhipu_cfg), 1.0)])
    return re


@pytest_asyncio.fixture
async def anthropic_app_services(
    anthropic_compat_router, mock_db_logger, mock_operational_store, mock_log_store
):
    """AppServices instance for anthropic compat router tests."""
    from serving.servers.deps import AppServices

    return AppServices(
        router=anthropic_compat_router,
        db_logger=mock_db_logger,
        operational_store=mock_operational_store,
        log_store=mock_log_store,
        routing_manager=None,
    )


@pytest_asyncio.fixture
async def anthropic_test_app(anthropic_app_services):
    """Test FastAPI app with the anthropic_messages router mounted and stubbed
    auth that accepts ANTHROPIC_TEST_API_KEY.
    """
    from contextlib import asynccontextmanager

    from fastapi import FastAPI, Header, HTTPException

    @asynccontextmanager
    async def lifespan(app):
        app.state.services = anthropic_app_services
        yield

    app = FastAPI(title="Anthropic Compat Test App", lifespan=lifespan)
    app.state.services = anthropic_app_services

    from serving.servers.auth import verify_api_key

    async def fake_verify(
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None, alias="X-API-Key"),
    ):
        token = None
        if authorization and authorization.startswith("Bearer "):
            token = authorization[len("Bearer ") :]
        elif x_api_key:
            token = x_api_key
        if token != ANTHROPIC_TEST_API_KEY:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return {"authenticated": True, "user_id": "test-user", "role": "internal"}

    app.dependency_overrides[verify_api_key] = fake_verify

    from serving.servers.concurrency import enforce_user_concurrency

    app.dependency_overrides[enforce_user_concurrency] = lambda: None

    from serving.servers.routers import anthropic_messages

    app.include_router(anthropic_messages.router)

    app.add_exception_handler(
        HTTPException,
        anthropic_messages.anthropic_aware_http_exception_handler,
    )
    return app


@pytest_asyncio.fixture
async def anthropic_test_client(anthropic_test_app):
    """Async HTTP client for the anthropic compat test app."""
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=anthropic_test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
