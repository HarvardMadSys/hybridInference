"""Tests for bootstrap module."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root to Python path
project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

import pytest

from routing.executor import RouteExecutor
from routing.manager import RoutingManager
from serving.config.model_visibility import ModelVisibilityResolver
from serving.servers import bootstrap
from serving.servers.deps import AppServices


class TestBootstrapInitialization:
    """Test bootstrap initialization functions."""

    @pytest.mark.asyncio
    async def test_initialize_returns_app_services(self, mock_env):
        """Test that initialize returns properly typed AppServices."""
        with (
            patch("serving.servers.bootstrap._init_db_logger", return_value=None),
            patch(
                "serving.servers.bootstrap._init_router_and_models",
                new=AsyncMock(return_value=({}, [])),
            ),
            patch("serving.servers.bootstrap._apply_routing_manager", return_value=None),
        ):
            services = await bootstrap.initialize()

            assert isinstance(services, AppServices)
            assert isinstance(services.router, RouteExecutor)
            assert services.db_logger is None  # Disabled in mock_env
            assert services.routing_manager is None
            assert services.model_visibility_resolver is None

    @pytest.mark.asyncio
    async def test_initialize_with_database(self, mock_env, monkeypatch):
        """Test initialization with PostgreSQL database enabled."""
        monkeypatch.setenv("DB_ENABLED", "true")
        monkeypatch.setenv("DB_HOST", "testhost")
        monkeypatch.setenv("DB_NAME", "testdb")
        monkeypatch.setenv("DB_USER", "testuser")
        monkeypatch.setenv("DB_PASSWORD", "testpass")

        with (
            patch(
                "serving.servers.bootstrap._init_router_and_models",
                new=AsyncMock(return_value=({}, [])),
            ),
            patch("serving.servers.bootstrap._apply_routing_manager", return_value=None),
            patch("serving.servers.bootstrap.DatabaseLogger") as MockDBLogger,
            patch("serving.servers.bootstrap.PostgresOperationalStore") as MockPGOp,
        ):
            mock_logger = AsyncMock()
            MockDBLogger.return_value = mock_logger

            mock_pg_op = AsyncMock()
            MockPGOp.return_value = mock_pg_op

            services = await bootstrap.initialize()

            assert services.db_logger is not None
            mock_logger.initialize.assert_called_once()
            mock_pg_op.initialize.assert_called_once()
            assert isinstance(services.model_visibility_resolver, ModelVisibilityResolver)

    @pytest.mark.asyncio
    async def test_initialize_attaches_model_visibility_resolver_when_operational_store_exists(
        self, mock_env
    ):
        """Operational store-backed boot should expose a model visibility resolver."""
        mock_db_logger = AsyncMock()
        mock_db_logger.pool = object()

        with (
            patch("serving.servers.bootstrap._init_db_logger", return_value=mock_db_logger),
            patch(
                "serving.servers.bootstrap._init_router_and_models",
                new=AsyncMock(return_value=({}, [])),
            ),
            patch("serving.servers.bootstrap._apply_routing_manager", return_value=None),
            patch("serving.servers.bootstrap.PostgresOperationalStore") as MockPGOp,
            patch("serving.servers.bootstrap.CachedOperationalStore") as MockCachedStore,
        ):
            mock_pg_op = AsyncMock()
            MockPGOp.return_value = mock_pg_op
            cached_store = MagicMock()
            MockCachedStore.return_value = cached_store

            services = await bootstrap.initialize()

            assert services.operational_store is cached_store
            assert isinstance(services.model_visibility_resolver, ModelVisibilityResolver)
            assert services.model_visibility_resolver._store is cached_store

    @pytest.mark.asyncio
    async def test_initialize_logs_warning_when_model_visibility_resolver_init_fails(
        self, mock_env
    ):
        """Resolver initialization failures should be non-fatal."""
        mock_db_logger = AsyncMock()
        mock_db_logger.pool = object()

        with (
            patch("serving.servers.bootstrap._init_db_logger", return_value=mock_db_logger),
            patch(
                "serving.servers.bootstrap._init_router_and_models",
                new=AsyncMock(return_value=({}, [])),
            ),
            patch("serving.servers.bootstrap._apply_routing_manager", return_value=None),
            patch("serving.servers.bootstrap.PostgresOperationalStore") as MockPGOp,
            patch("serving.servers.bootstrap.CachedOperationalStore") as MockCachedStore,
            patch(
                "serving.servers.bootstrap.ModelVisibilityResolver",
                side_effect=RuntimeError("boom"),
            ),
            patch("serving.servers.bootstrap.logger") as mock_logger,
        ):
            mock_pg_op = AsyncMock()
            MockPGOp.return_value = mock_pg_op
            MockCachedStore.return_value = MagicMock()

            services = await bootstrap.initialize()

            assert services.model_visibility_resolver is None
            mock_logger.warning.assert_any_call(
                "Model visibility resolver initialization failed: boom"
            )

    @pytest.mark.asyncio
    async def test_initialize_loads_models_yaml(self, mock_env, temp_models_yaml, monkeypatch):
        """Test that models.yaml is loaded and registered."""
        monkeypatch.setenv("MODELS_CONFIG", temp_models_yaml)
        monkeypatch.setenv("LOCAL_DEPLOYMENT_URL", "http://localhost:8001")

        with (
            patch("serving.servers.bootstrap._init_db_logger", return_value=None),
            patch("serving.servers.bootstrap._apply_routing_manager", return_value=None),
        ):
            services = await bootstrap.initialize()

            # Check that routes were registered
            assert len(services.router.routes) > 0
            assert "test-model-1" in services.router.routes
            assert "test-alias-1" in services.router.routes

    @pytest.mark.asyncio
    async def test_initialize_constructs_user_concurrency_limiter(self, mock_env):
        """services.user_concurrency_limiter must be a UserConcurrencyLimiter
        with caps for free/pro/internal/admin."""
        from serving.servers.concurrency import UserConcurrencyLimiter

        with (
            patch("serving.servers.bootstrap._init_db_logger", return_value=None),
            patch(
                "serving.servers.bootstrap._init_router_and_models",
                new=AsyncMock(return_value=({}, [])),
            ),
            patch("serving.servers.bootstrap._apply_routing_manager", return_value=None),
        ):
            services = await bootstrap.initialize()

            assert isinstance(services.user_concurrency_limiter, UserConcurrencyLimiter)
            limiter = services.user_concurrency_limiter
            for role in ("trial", "free", "pro", "internal", "admin"):
                granted, cap, _ = await limiter.try_acquire(f"u-{role}", role, False)
                assert granted
                assert cap >= 1
            # is_admin=True must yield admin cap
            granted, cap, label = await limiter.try_acquire("admin-user", "free", True)
            assert granted
            assert cap == 10
            assert label == "admin"

    @pytest.mark.asyncio
    async def test_initialize_with_routing_manager(self, mock_env, temp_routing_yaml, monkeypatch):
        """Test initialization with routing manager."""
        monkeypatch.setenv("ROUTING_CONFIG", temp_routing_yaml)
        monkeypatch.setenv("LOCAL_DEPLOYMENT_URL", "http://localhost:8001")

        with (
            patch("serving.servers.bootstrap._init_db_logger", return_value=None),
            patch(
                "serving.servers.bootstrap._init_router_and_models",
                new=AsyncMock(return_value=({}, [])),
            ),
        ):
            services = await bootstrap.initialize()

            # Routing manager should be initialized
            assert services.routing_manager is not None

    @pytest.mark.asyncio
    async def test_initialize_does_not_start_routewise_until_bootstrap_succeeds(self, mock_env):
        """A later bootstrap failure must not leave the RouteWise sweep task running."""
        mock_routewise = MagicMock()
        mock_routewise.start = AsyncMock()

        info = MagicMock()
        info.model_id = "m"
        info.aliases = []
        info.router = "routewise"
        info.strategy = None
        info.router_params = None

        # Make the registry hand out our mock RouteWiseRouter so we can assert
        # on its lifecycle.
        registry_instance = MagicMock()
        registry_instance.bind_fixed_router = MagicMock()
        registry_instance.get_router = MagicMock(return_value=mock_routewise)
        # type(...).__name__ == "RouteWiseRouter" check in bootstrap relies on
        # the class name; using a real subclass keeps that branch honest.
        from routing.routewise.router import RouteWiseRouter as _RWR

        mock_routewise.__class__ = _RWR

        with (
            patch("serving.servers.bootstrap._init_db_logger", return_value=None),
            patch(
                "serving.servers.bootstrap._init_router_and_models",
                new=AsyncMock(return_value=({}, [info])),
            ),
            patch("serving.servers.bootstrap._apply_routing_manager", return_value=None),
            patch(
                "serving.servers.bootstrap.ModelRouterRegistry",
                return_value=registry_instance,
            ),
            patch(
                "serving.servers.bootstrap.AsyncHTTPClient.shared",
                side_effect=RuntimeError("boom after routewise init"),
            ),
            pytest.raises(RuntimeError, match="boom after routewise init"),
        ):
            await bootstrap.initialize()

        mock_routewise.start.assert_not_called()

    @pytest.mark.asyncio
    async def test_routewise_db_bootstrap_replays_recent_logs(self):
        """RouteWise DB bootstrap queries assigned model ids and replays rows."""
        log_store = AsyncMock()
        log_store.get_routewise_bootstrap_rows.side_effect = [
            [{"model_id": "m", "source": "latency"}],
            [{"model_id": "m", "source": "envelope"}],
        ]
        rw = MagicMock()
        rw.config = SimpleNamespace(
            db_bootstrap_enabled=True,
            db_bootstrap_max_rows=123,
            latency_window_sec=900.0,
            envelope_window_hours=24,
        )
        rw.bootstrap_from_log_rows.return_value = {
            "rows": 1,
            "latency_events": 1,
            "failed_attempts": 0,
            "envelope_samples": 1,
        }

        await bootstrap._bootstrap_routewise_from_logs(
            log_store,
            [rw],
            {id(rw): {"m", "alias"}},
        )

        assert log_store.get_routewise_bootstrap_rows.await_count == 2
        latency_call = log_store.get_routewise_bootstrap_rows.await_args_list[0].kwargs
        envelope_call = log_store.get_routewise_bootstrap_rows.await_args_list[1].kwargs
        assert latency_call["model_ids"] == ["alias", "m"]
        assert envelope_call["model_ids"] == ["alias", "m"]
        assert latency_call["limit"] == 123
        assert envelope_call["limit"] is None
        assert (envelope_call["since"] - latency_call["since"]).total_seconds() < 0
        rw.bootstrap_from_log_rows.assert_any_call(
            [{"model_id": "m", "source": "latency"}],
            include_latency=True,
            include_envelope=False,
        )
        rw.bootstrap_from_log_rows.assert_any_call(
            [{"model_id": "m", "source": "envelope"}],
            include_latency=False,
            include_envelope=True,
            envelope_model_overrides={},
        )

    @pytest.mark.asyncio
    async def test_routewise_db_bootstrap_includes_envelope_donor_models(self):
        """Donor model ids widen the envelope query and map onto the target."""
        log_store = AsyncMock()
        log_store.get_routewise_bootstrap_rows.side_effect = [
            [{"model_id": "m", "source": "latency"}],
            [{"model_id": "donor", "source": "envelope"}],
        ]
        rw = MagicMock()
        rw.config = SimpleNamespace(
            db_bootstrap_enabled=True,
            db_bootstrap_max_rows=123,
            latency_window_sec=900.0,
            envelope_window_hours=24,
        )
        rw.bootstrap_from_log_rows.return_value = {
            "rows": 1,
            "latency_events": 0,
            "failed_attempts": 0,
            "envelope_samples": 1,
        }

        await bootstrap._bootstrap_routewise_from_logs(
            log_store,
            [rw],
            {id(rw): {"m"}},
            {id(rw): {"donor": "m", "Donor-Alias": "m"}},
        )

        latency_call = log_store.get_routewise_bootstrap_rows.await_args_list[0].kwargs
        envelope_call = log_store.get_routewise_bootstrap_rows.await_args_list[1].kwargs
        # Latency stays own-model; envelope query includes donors + aliases.
        assert latency_call["model_ids"] == ["m"]
        assert envelope_call["model_ids"] == ["Donor-Alias", "donor", "m"]
        rw.bootstrap_from_log_rows.assert_any_call(
            [{"model_id": "donor", "source": "envelope"}],
            include_latency=False,
            include_envelope=True,
            envelope_model_overrides={"donor": "m", "Donor-Alias": "m"},
        )

    @pytest.mark.asyncio
    async def test_routewise_db_bootstrap_swallow_query_failures(self):
        """RouteWise DB bootstrap is best-effort and must not block startup."""
        log_store = AsyncMock()
        log_store.get_routewise_bootstrap_rows.side_effect = RuntimeError("db down")
        rw = MagicMock()
        rw.config = SimpleNamespace(
            db_bootstrap_enabled=True,
            db_bootstrap_max_rows=123,
            latency_window_sec=900.0,
            envelope_window_hours=24,
        )

        await bootstrap._bootstrap_routewise_from_logs(
            log_store,
            [rw],
            {id(rw): {"m"}},
        )

        log_store.get_routewise_bootstrap_rows.assert_awaited_once()
        rw.bootstrap_from_log_rows.assert_not_called()

    @pytest.mark.asyncio
    async def test_routewise_db_bootstrap_keeps_latency_when_envelope_fetch_fails(self):
        """Latency warmup survives a later envelope bootstrap query failure."""
        log_store = AsyncMock()
        log_store.get_routewise_bootstrap_rows.side_effect = [
            [{"model_id": "m", "source": "latency"}],
            RuntimeError("db blip"),
        ]
        rw = MagicMock()
        rw.config = SimpleNamespace(
            db_bootstrap_enabled=True,
            db_bootstrap_max_rows=123,
            latency_window_sec=900.0,
            envelope_window_hours=24,
        )
        rw.bootstrap_from_log_rows.return_value = {
            "rows": 1,
            "latency_events": 1,
            "failed_attempts": 0,
            "envelope_samples": 0,
        }

        await bootstrap._bootstrap_routewise_from_logs(
            log_store,
            [rw],
            {id(rw): {"m"}},
        )

        assert log_store.get_routewise_bootstrap_rows.await_count == 2
        rw.bootstrap_from_log_rows.assert_called_once_with(
            [{"model_id": "m", "source": "latency"}],
            include_latency=True,
            include_envelope=False,
        )

    @pytest.mark.asyncio
    async def test_envelope_not_calibrated_aborts_initialization(self, mock_env):
        """An uncalibrated quota envelope must fail boot, not silently start."""
        from routing.routewise.envelope import EnvelopeNotCalibratedError

        mock_routewise = MagicMock()
        mock_routewise.start = AsyncMock(
            side_effect=EnvelopeNotCalibratedError("envelope is uncalibrated")
        )

        info = MagicMock()
        info.model_id = "m"
        info.aliases = []
        info.router = "routewise"
        info.strategy = None
        info.router_params = None

        registry_instance = MagicMock()
        registry_instance.bind_fixed_router = MagicMock()
        registry_instance.get_router = MagicMock(return_value=mock_routewise)
        from routing.routewise.router import RouteWiseRouter as _RWR

        mock_routewise.__class__ = _RWR

        with (
            patch("serving.servers.bootstrap._init_db_logger", return_value=None),
            patch(
                "serving.servers.bootstrap._init_router_and_models",
                new=AsyncMock(return_value=({}, [info])),
            ),
            patch("serving.servers.bootstrap._apply_routing_manager", return_value=None),
            patch(
                "serving.servers.bootstrap.ModelRouterRegistry",
                return_value=registry_instance,
            ),
            patch(
                "serving.servers.bootstrap._bootstrap_routewise_from_logs",
                new=AsyncMock(),
            ),
            pytest.raises(EnvelopeNotCalibratedError, match="uncalibrated"),
        ):
            await bootstrap.initialize()

    @pytest.mark.asyncio
    async def test_failed_runtime_router_override_resets_to_configured_strategy(self):
        """A stale DB-backed strategy override should not brick startup."""
        managed_router = SimpleNamespace(
            _model_router_override_id="m",
            _model_router_fallback_strategy="fixed",
        )
        managed_routers = [managed_router]
        fallback_router = object()
        registry = MagicMock()
        registry.get_router.return_value = fallback_router
        op_store = AsyncMock()

        reset = await bootstrap._reset_failed_runtime_router_override(
            managed_router=managed_router,
            model_router_registry=registry,
            managed_routers=managed_routers,
            operational_store=op_store,
        )

        assert reset is True
        registry.set_router_override.assert_called_once_with("m", "fixed")
        registry.get_router.assert_called_once_with("m")
        op_store.set_setting.assert_awaited_once_with(
            "model_router_strategy:m",
            "fixed",
            "string",
            "bootstrap",
        )
        assert managed_routers == []


class TestBootstrapShutdown:
    """Test bootstrap shutdown functionality."""

    @pytest.mark.asyncio
    async def test_shutdown_cleans_up_resources(self, app_services):
        """Test that shutdown properly cleans up all resources."""
        # Mock cleanup methods
        app_services.db_logger.cleanup = AsyncMock()

        # Add routing manager with health monitor
        routing_manager = MagicMock()
        routing_manager.shutdown = AsyncMock()
        app_services.routing_manager = routing_manager

        await bootstrap.shutdown(app_services)

        # Verify cleanup was called
        app_services.db_logger.cleanup.assert_called_once()
        routing_manager.shutdown.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_handles_none_services(self):
        """Test that shutdown handles None values gracefully."""
        services = AppServices(router=RouteExecutor(), db_logger=None, routing_manager=None)

        # Should not raise any errors
        await bootstrap.shutdown(services)

    @pytest.mark.asyncio
    async def test_shutdown_handles_exceptions(self, app_services):
        """Test that shutdown continues even if cleanup fails."""
        # Make cleanup raise an exception
        app_services.db_logger.cleanup = AsyncMock(side_effect=Exception("DB cleanup failed"))

        # Should not raise, but should log the error
        with patch("serving.servers.bootstrap.logger") as mock_logger:
            await bootstrap.shutdown(app_services)

            # Check that error was logged
            mock_logger.error.assert_called()


class TestBootstrapHelpers:
    """Test bootstrap helper functions."""

    def test_init_db_logger_postgres(self, monkeypatch):
        """Test PostgreSQL database logger initialization."""
        monkeypatch.setenv("DB_HOST", "testhost")
        monkeypatch.setenv("DB_PORT", "5433")
        monkeypatch.setenv("DB_NAME", "testdb")
        monkeypatch.setenv("DB_USER", "testuser")
        monkeypatch.setenv("DB_PASSWORD", "testpass")

        # Clear settings cache to pick up new environment variables
        from serving.config.settings import get_settings

        get_settings.cache_clear()

        logger = bootstrap._init_db_logger()

        assert logger is not None
        assert isinstance(logger, bootstrap.DatabaseLogger)
        assert logger.db_config["host"] == "testhost"
        assert logger.db_config["port"] == 5433
        assert logger.db_config["database"] == "testdb"
        assert logger.db_config["user"] == "testuser"
        assert logger.db_config["password"] == "testpass"

    def test_init_db_logger_disabled(self, monkeypatch):
        """Test database logger when disabled."""
        monkeypatch.setenv("DB_ENABLED", "false")

        logger = bootstrap._init_db_logger()

        assert logger is None

    @pytest.mark.asyncio
    async def test_init_router_with_remote_models(self, monkeypatch, tmp_path):
        """Router should register purely remote models from YAML."""
        models_yaml = tmp_path / "remote_models.yaml"
        models_yaml.write_text(
            """
models:
  - id: remote-model
    name: Remote Only Model
    provider: zai
    context_length: 8192
    max_output_length: 4096
    route:
      - kind: zai
        weight: 1.0
        base_url: https://api.example.com
        api_key: test-key
"""
        )

        monkeypatch.setenv("MODELS_CONFIG", str(models_yaml))

        router = RouteExecutor()

        await bootstrap._init_router_and_models(router)

        assert "remote-model" in router.routes
        assert len(router.routes["remote-model"].adapters) == 1

    @pytest.mark.asyncio
    async def test_init_router_with_deepseek(self, monkeypatch, tmp_path):
        """DeepSeek should be loaded from YAML, not env fallback."""
        models_yaml = tmp_path / "models_deepseek.yaml"
        models_yaml.write_text(
            """
models:
  - id: deepseek-chat
    name: DeepSeek Chat
    provider: deepseek
    base_url: https://api.deepseek.com/v1
    api_key: test-key
    context_length: 65536
    max_output_length: 8192
    supports_tools: true
    supports_structured_output: true
    supported_params: [temperature, top_p, max_tokens, stop, frequency_penalty, presence_penalty]
    route:
      - kind: deepseek
        weight: 1.0
        base_url: https://api.deepseek.com/v1
        api_key: test-key
"""
        )
        monkeypatch.setenv("MODELS_CONFIG", str(models_yaml))

        router = RouteExecutor()
        await bootstrap._init_router_and_models(router)
        assert "deepseek-chat" in router.routes

    @pytest.mark.asyncio
    async def test_init_router_with_gemini(self, monkeypatch, tmp_path):
        """Gemini should be loaded from YAML, not env fallback."""
        models_yaml = tmp_path / "models_gemini.yaml"
        models_yaml.write_text(
            """
models:
  - id: gemini-2.5-flash
    name: Gemini 2.5 Flash
    provider: gemini
    provider_model_id: "gemini-2.5-flash"
    base_url: https://generativelanguage.googleapis.com/v1beta
    api_key: test-key
    context_length: 1048576
    max_output_length: 8192
    supports_tools: true
    supports_structured_output: true
    supported_params: [temperature, top_p, top_k, max_tokens, stop]
    route:
      - kind: gemini
        weight: 1.0
        base_url: https://generativelanguage.googleapis.com/v1beta
        api_key: test-key
        provider_model_id: "gemini-2.5-flash"
"""
        )
        monkeypatch.setenv("MODELS_CONFIG", str(models_yaml))

        router = RouteExecutor()
        await bootstrap._init_router_and_models(router)
        assert "gemini-2.5-flash" in router.routes


class TestBootstrapErrorHandling:
    """Test error handling in bootstrap."""

    @pytest.mark.asyncio
    async def test_initialize_handles_model_yaml_error(self, mock_env, monkeypatch):
        """Test that initialize continues if models.yaml fails to load."""
        monkeypatch.setenv("MODELS_CONFIG", "/nonexistent/models.yaml")

        with (
            patch("serving.servers.bootstrap._init_db_logger", return_value=None),
            patch("serving.servers.bootstrap._apply_routing_manager", return_value=None),
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
            patch(
                "serving.servers.bootstrap._init_router_and_models",
                new=AsyncMock(return_value=({}, [])),
            ),
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
