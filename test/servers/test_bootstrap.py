"""Tests for bootstrap module."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root to Python path
project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

import pytest

from routing.executor import RouteExecutor
from routing.manager import RoutingManager
from serving.servers import bootstrap
from serving.servers.deps import AppServices


class TestBootstrapInitialization:
    """Test bootstrap initialization functions."""

    @pytest.mark.asyncio
    async def test_initialize_returns_app_services(self, mock_env):
        """Test that initialize returns properly typed AppServices."""
        with (
            patch("serving.servers.bootstrap._init_db_logger", return_value=None),
            patch("serving.servers.bootstrap._init_router_and_models", new=AsyncMock()),
            patch("serving.servers.bootstrap._apply_routing_manager", return_value=None),
            patch("serving.servers.bootstrap._configure_rate_limiter"),
        ):
            services = await bootstrap.initialize()

            assert isinstance(services, AppServices)
            assert isinstance(services.router, RouteExecutor)
            assert services.db_logger is None  # Disabled in mock_env
            assert services.rate_limiter is None  # Disabled in mock_env
            assert services.routing_manager is None

    @pytest.mark.asyncio
    async def test_initialize_with_database(self, mock_env, temp_db_path, monkeypatch):
        """Test initialization with SQLite database enabled."""
        monkeypatch.setenv("USE_SQLITE_LOG", "true")
        monkeypatch.setenv("SQLITE_DB_PATH", temp_db_path)

        with (
            patch("serving.servers.bootstrap._init_router_and_models", new=AsyncMock()),
            patch("serving.servers.bootstrap._apply_routing_manager", return_value=None),
            patch("serving.servers.bootstrap._configure_rate_limiter"),
        ):
            services = await bootstrap.initialize()

            assert services.db_logger is not None
            await services.db_logger.cleanup()

    @pytest.mark.asyncio
    async def test_initialize_with_rate_limiter(self, mock_env, monkeypatch):
        """Test initialization with rate limiter enabled."""
        monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")

        with (
            patch("serving.servers.bootstrap._init_db_logger", return_value=None),
            patch("serving.servers.bootstrap._init_router_and_models", new=AsyncMock()),
            patch("serving.servers.bootstrap._apply_routing_manager", return_value=None),
        ):
            services = await bootstrap.initialize()

            assert services.rate_limiter is not None
            await services.rate_limiter._persist_state()

    @pytest.mark.asyncio
    async def test_initialize_loads_models_yaml(self, mock_env, temp_models_yaml, monkeypatch):
        """Test that models.yaml is loaded and registered."""
        monkeypatch.setenv("MODELS_CONFIG", temp_models_yaml)
        monkeypatch.setenv("LOCAL_BASE_URL", "http://localhost:8001")

        with (
            patch("serving.servers.bootstrap._init_db_logger", return_value=None),
            patch("serving.servers.bootstrap._apply_routing_manager", return_value=None),
            patch("serving.servers.bootstrap._configure_rate_limiter"),
        ):
            services = await bootstrap.initialize()

            # Check that routes were registered
            assert len(services.router.routes) > 0
            assert "test-model-1" in services.router.routes
            assert "test-alias-1" in services.router.routes

    @pytest.mark.asyncio
    async def test_initialize_with_routing_manager(self, mock_env, temp_routing_yaml, monkeypatch):
        """Test initialization with routing manager."""
        monkeypatch.setenv("ROUTING_CONFIG", temp_routing_yaml)
        monkeypatch.setenv("LOCAL_BASE_URL", "http://localhost:8001")

        with (
            patch("serving.servers.bootstrap._init_db_logger", return_value=None),
            patch("serving.servers.bootstrap._init_router_and_models", new=AsyncMock()),
            patch("serving.servers.bootstrap._configure_rate_limiter"),
        ):
            services = await bootstrap.initialize()

            # Routing manager should be initialized
            assert services.routing_manager is not None


class TestBootstrapShutdown:
    """Test bootstrap shutdown functionality."""

    @pytest.mark.asyncio
    async def test_shutdown_cleans_up_resources(self, app_services):
        """Test that shutdown properly cleans up all resources."""
        # Mock cleanup methods
        app_services.db_logger.cleanup = AsyncMock()
        app_services.rate_limiter._persist_state = AsyncMock()

        # Add routing manager with health monitor
        routing_manager = MagicMock()
        routing_manager.shutdown = AsyncMock()
        app_services.routing_manager = routing_manager

        await bootstrap.shutdown(app_services)

        # Verify cleanup was called
        app_services.db_logger.cleanup.assert_called_once()
        app_services.rate_limiter._persist_state.assert_called_once()
        routing_manager.shutdown.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_handles_none_services(self):
        """Test that shutdown handles None values gracefully."""
        services = AppServices(
            router=RouteExecutor(), db_logger=None, rate_limiter=None, routing_manager=None
        )

        # Should not raise any errors
        await bootstrap.shutdown(services)

    @pytest.mark.asyncio
    async def test_shutdown_handles_exceptions(self, app_services):
        """Test that shutdown continues even if cleanup fails."""
        # Make cleanup raise an exception
        app_services.db_logger.cleanup = AsyncMock(side_effect=Exception("DB cleanup failed"))
        app_services.rate_limiter._persist_state = AsyncMock()

        # Should not raise, but should log the error
        with patch("serving.servers.bootstrap.logger") as mock_logger:
            await bootstrap.shutdown(app_services)

            # Check that error was logged
            mock_logger.error.assert_called()
            # But other cleanup still happened
            app_services.rate_limiter._persist_state.assert_called_once()


class TestBootstrapHelpers:
    """Test bootstrap helper functions."""

    def test_init_db_logger_sqlite(self, monkeypatch, temp_db_path):
        """Test SQLite database logger initialization."""
        monkeypatch.setenv("USE_SQLITE_LOG", "true")
        monkeypatch.setenv("SQLITE_DB_PATH", temp_db_path)

        logger = bootstrap._init_db_logger()

        assert logger is not None
        assert temp_db_path in str(logger.db_path)

    def test_init_db_logger_disabled(self, monkeypatch):
        """Test database logger when disabled."""
        monkeypatch.setenv("USE_SQLITE_LOG", "false")

        logger = bootstrap._init_db_logger()

        assert logger is None

    def test_init_db_logger_postgres(self, monkeypatch):
        """Test PostgreSQL database logger initialization."""
        monkeypatch.setenv("USE_SQLITE_LOG", "false")
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")

        with patch("serving.servers.bootstrap.DatabaseLogger") as MockDBLogger:
            logger = bootstrap._init_db_logger()

            MockDBLogger.assert_called_once()
            assert logger is not None

    @pytest.mark.asyncio
    async def test_init_router_with_local_models(self, monkeypatch):
        """Test router initialization with local VLLM models."""
        monkeypatch.setenv("LOCAL_BASE_URL", "http://localhost:8001")
        monkeypatch.setenv("OFFLOAD", "0")

        router = RouteExecutor()

        await bootstrap._init_router_and_models(router)

        # Should have registered local models
        assert "llama-4-scout" in router.routes
        assert "qwen3-coder" in router.routes

    @pytest.mark.asyncio
    async def test_init_router_with_offload(self, monkeypatch):
        """Test router skips local models when OFFLOAD=1."""
        monkeypatch.setenv("LOCAL_BASE_URL", "http://localhost:8001")
        monkeypatch.setenv("OFFLOAD", "1")

        router = RouteExecutor()

        await bootstrap._init_router_and_models(router)

        # Should NOT have registered local models
        assert "llama-4-scout" not in router.routes
        assert "qwen3-coder" not in router.routes

    @pytest.mark.asyncio
    async def test_init_router_with_deepseek(self, monkeypatch):
        """Test router initialization with DeepSeek API."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

        router = RouteExecutor()

        await bootstrap._init_router_and_models(router)

        assert "deepseek-chat" in router.routes

    @pytest.mark.asyncio
    async def test_init_router_with_gemini(self, monkeypatch):
        """Test router initialization with Gemini API."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        router = RouteExecutor()

        await bootstrap._init_router_and_models(router)

        assert "gemini-2.5-flash" in router.routes

    def test_configure_rate_limiter_deepseek(self, monkeypatch, mock_rate_limiter):
        """Test rate limiter configuration for DeepSeek."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        monkeypatch.setenv("DEEPSEEK_TPD_LIMIT", "500000")

        bootstrap._configure_rate_limiter(mock_rate_limiter)

        mock_rate_limiter.configure.assert_called()
        config = mock_rate_limiter.configure.call_args[0][0]
        assert config.model_id == "deepseek-chat"
        assert config.capacity_tokens == 500000
        assert config.window_seconds == 86400

    def test_configure_rate_limiter_gemini(self, monkeypatch, mock_rate_limiter):
        """Test rate limiter configuration for Gemini."""
        # Clear any existing keys first
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("GEMINI_TPM_LIMIT", "2000000")

        bootstrap._configure_rate_limiter(mock_rate_limiter)

        # Check that configure was called for Gemini
        mock_rate_limiter.configure.assert_called()
        # Find the call with gemini model_id
        calls = mock_rate_limiter.configure.call_args_list
        gemini_config = None
        for call in calls:
            config = call[0][0]
            if config.model_id == "gemini-2.5-flash":
                gemini_config = config
                break

        assert gemini_config is not None, "Gemini config not found"
        assert gemini_config.capacity_tokens == 2000000
        assert gemini_config.window_seconds == 60


class TestBootstrapErrorHandling:
    """Test error handling in bootstrap."""

    @pytest.mark.asyncio
    async def test_initialize_handles_model_yaml_error(self, mock_env, monkeypatch):
        """Test that initialize continues if models.yaml fails to load."""
        monkeypatch.setenv("MODELS_CONFIG", "/nonexistent/models.yaml")

        with (
            patch("serving.servers.bootstrap._init_db_logger", return_value=None),
            patch("serving.servers.bootstrap._apply_routing_manager", return_value=None),
            patch("serving.servers.bootstrap._configure_rate_limiter"),
            patch("serving.servers.bootstrap.logger") as mock_logger,
        ):
            services = await bootstrap.initialize()

            # Should still return services
            assert isinstance(services, AppServices)
            # Should log a warning about missing models config
            mock_logger.warning.assert_called()

    @pytest.mark.asyncio
    async def test_initialize_handles_routing_manager_error(self, mock_env, monkeypatch):
        """Test that initialize continues if routing manager fails."""
        monkeypatch.setenv("ROUTING_CONFIG", "/invalid/routing.yaml")

        with (
            patch("serving.servers.bootstrap._init_db_logger", return_value=None),
            patch("serving.servers.bootstrap._init_router_and_models", new=AsyncMock()),
            patch("serving.servers.bootstrap._configure_rate_limiter"),
            patch("serving.servers.bootstrap.logger") as mock_logger,
        ):
            services = await bootstrap.initialize()

            # Should still return services
            assert isinstance(services, AppServices)
            # Routing manager is optional, so None is acceptable
            assert services.routing_manager is None or isinstance(
                services.routing_manager, RoutingManager
            )
            # Should log a warning about routing config failure/missing
            mock_logger.warning.assert_called()
