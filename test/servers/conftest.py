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


@pytest.fixture(scope="session", autouse=True)
def auth_test_env():
    """Set required environment variables for auth tests.
    
    This fixture runs automatically and ensures auth-related
    environment variables are set for all tests.
    """
    os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-32-chars-long!!")
    os.environ.setdefault("API_KEY_SECRET", "test-api-key-secret")
    os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
    os.environ.setdefault("BASE_URL", "http://test")
    os.environ.setdefault("COOKIE_SECURE", "0")
    os.environ.setdefault("SIGNUP_ENABLED", "1")
    os.environ.setdefault("SIGNUP_DEFAULT_DAILY_QUOTA_USD", "10.00")
    os.environ.setdefault("SIGNUP_REQUIRE_EMAIL_VERIFICATION", "0")


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

    # Import and register routes from new modular structure
    from serving.servers.routers import health, models

    # Include routers (they have their own routes defined)
    app.include_router(health.router)
    app.include_router(models.router)

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
async def auth_client():
    """Async HTTP client with FastAPI lifespan enabled for auth tests.
    
    CRITICAL: Uses ASGITransport with lifespan="on" to ensure
    app.state.services is initialized before tests run.
    
    This client uses the REAL app with real database and services,
    unlike test_client which uses mocks.
    
    Usage:
        async def test_login(auth_client):
            response = await auth_client.post("/auth/login", json={...})
            assert response.status_code == 200
    """
    from serving.servers.app import app
    from httpx import ASGITransport
    
    # CRITICAL: lifespan="on" ensures app.state.services is initialized
    transport = ASGITransport(app=app, lifespan="on")
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def auth_db_logger(auth_client):
    """Database logger from app state (initialized in lifespan).
    
    CRITICAL: Access via app.state.services, not Depends(get_db_logger).
    Depends() functions cannot be called directly in tests.
    
    Note: This fixture depends on 'auth_client' fixture to ensure lifespan
    has run and app.state.services is populated.
    
    Usage:
        async def test_example(auth_client, auth_db_logger):
            async with auth_db_logger.pool.acquire() as conn:
                result = await conn.fetchrow("SELECT 1")
    """
    from serving.servers.app import app
    
    # CRITICAL: Access from app.state.services, not by calling get_db_logger()
    return app.state.services.db_logger  # type: ignore[attr-defined]


@pytest_asyncio.fixture
async def require_db(auth_db_logger):
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
    if auth_db_logger is None or not hasattr(auth_db_logger, 'pool') or auth_db_logger.pool is None:
        pytest.skip("Database not available (start PostgreSQL or check DB config)")
    return auth_db_logger


@pytest_asyncio.fixture
async def test_user(auth_client):
    """Create a test user for auth tests."""
    user_data = {
        "email": f"test_{os.urandom(4).hex()}@example.com",
        "password": "TestPass123!",
        "user_name": "Test User"
    }
    
    response = await auth_client.post("/auth/signup", json=user_data)
    assert response.status_code == 201
    
    return {
        **user_data,
        "user_id": response.json()["user_id"]
    }


@pytest_asyncio.fixture
async def authenticated_user(auth_client, test_user):
    """Create and authenticate a test user."""
    login_response = await auth_client.post("/auth/login", json={
        "email": test_user["email"],
        "password": test_user["password"]
    })
    
    assert login_response.status_code == 200
    
    return {
        **test_user,
        "access_token": login_response.json()["access_token"]
    }


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
    provider: llama
    base_url: http://remote.test
    api_key: test-key
    context_length: 16384
    max_output_length: 8192
    aliases: ["test-alias-2"]
    route:
      - kind: llama
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
