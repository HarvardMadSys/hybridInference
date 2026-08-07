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
from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseRouter
from serving.agent_jobs import terminal_coordination
from serving.agent_jobs.workspace_broker_client import WorkspaceBrokerError
from serving.config.model_visibility import ModelVisibilityResolver
from serving.servers import bootstrap
from serving.servers.deps import AppServices
from serving.servers.registry import ModelRegistrationInfo


def _mock_routewise(*, config: RouteWiseConfig | None = None) -> MagicMock:
    """Return a strict RouteWise double with production-shaped configuration."""
    router = MagicMock(spec=RouteWiseRouter)
    router.config = config or RouteWiseConfig()
    return router


class TestBootstrapInitialization:
    """Test bootstrap initialization functions."""

    @pytest.mark.asyncio
    async def test_settled_terminal_resume_retries_until_broker_confirms(
        self,
        monkeypatch,
    ):
        broker = SimpleNamespace(
            resume_settled_terminals=AsyncMock(
                side_effect=[
                    WorkspaceBrokerError(503, "broker unavailable"),
                    {"ok": True},
                ]
            )
        )
        monkeypatch.setattr(terminal_coordination, "workspace_broker_from_env", lambda: broker)
        store = SimpleNamespace(
            list_terminal_resumes_pending=AsyncMock(return_value=["ajob_test"]),
            mark_terminal_resume_complete=AsyncMock(return_value=True),
        )

        await bootstrap._reconcile_settled_agent_terminals(store)
        store.mark_terminal_resume_complete.assert_not_awaited()

        await bootstrap._reconcile_settled_agent_terminals(store)
        store.mark_terminal_resume_complete.assert_awaited_once_with(job_id="ajob_test")
        assert broker.resume_settled_terminals.await_count == 2
        broker.resume_settled_terminals.assert_awaited_with("ajob_test")

    @pytest.mark.asyncio
    async def test_routewise_settings_apply_continues_after_one_router_fails(
        self,
        monkeypatch,
    ):
        resolver = MagicMock()
        registry = MagicMock()
        registry.configured_model_ids.return_value = ["model-a", "model-b"]
        registry.registered_models.return_value = {}
        registry.canonical_model_id.side_effect = lambda model_id: model_id
        routers = {
            "model-a": RouteWiseRouter(),
            "model-b": RouteWiseRouter(),
        }
        registry.get_cached_router.side_effect = routers.get
        apply_settings = AsyncMock(side_effect=[RuntimeError("model-a apply failed"), None])
        monkeypatch.setattr(
            bootstrap,
            "apply_routewise_settings_to_router",
            apply_settings,
        )

        with pytest.raises(RuntimeError, match="model-a apply failed"):
            await bootstrap._apply_cached_routewise_model_settings(
                resolver,
                registry,
                {},
                refresh_probe_task=True,
            )

        assert [call.args[2] for call in apply_settings.await_args_list] == [
            "model-a",
            "model-b",
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("changed", [False, True])
    async def test_reload_effective_route_state_rebuilds_only_on_change(
        self,
        monkeypatch,
        changed,
    ):
        resolver = SimpleNamespace(load_all=AsyncMock(return_value=changed))
        registry = MagicMock()
        rebuild = MagicMock()
        refresh_state = bootstrap._EffectiveRouteRefreshState()
        monkeypatch.setattr(bootstrap, "rebuild_cached_routewise_routers", rebuild)

        result = await bootstrap._reload_effective_route_state(
            resolver,
            registry,
            refresh_state,
        )

        assert result is changed
        resolver.load_all.assert_awaited_once_with()
        if changed:
            rebuild.assert_called_once_with(registry)
        else:
            rebuild.assert_not_called()

    @pytest.mark.asyncio
    async def test_reload_effective_route_state_retries_failed_rebuild(self, monkeypatch):
        resolver = SimpleNamespace(load_all=AsyncMock(side_effect=[True, False]))
        registry = MagicMock()
        rebuild = MagicMock(side_effect=[RuntimeError("rebuild failed"), None])
        refresh_state = bootstrap._EffectiveRouteRefreshState()
        monkeypatch.setattr(bootstrap, "rebuild_cached_routewise_routers", rebuild)

        with pytest.raises(RuntimeError, match="rebuild failed"):
            await bootstrap._reload_effective_route_state(
                resolver,
                registry,
                refresh_state,
            )

        assert refresh_state.rebuild_pending is True
        assert (
            await bootstrap._reload_effective_route_state(
                resolver,
                registry,
                refresh_state,
            )
            is True
        )
        assert refresh_state.rebuild_pending is False
        assert rebuild.call_count == 2

    @pytest.mark.asyncio
    async def test_initialize_returns_app_services(self, mock_env):
        """Test that initialize returns properly typed AppServices."""
        from routing.model_router_registry import ModelRouterRegistry

        with (
            patch("serving.servers.bootstrap._init_db_logger", return_value=None),
            patch(
                "serving.servers.bootstrap._init_router_and_models",
                new=AsyncMock(return_value=({}, [])),
            ),
            patch("serving.servers.bootstrap._apply_routing_manager", return_value=None),
            patch(
                "serving.servers.bootstrap.ModelRouterRegistry",
                wraps=ModelRouterRegistry,
            ) as registry_factory,
            patch.object(
                ModelRouterRegistry,
                "bind_fixed_router",
                side_effect=AssertionError("bootstrap must inject shared FixedRouter"),
            ),
        ):
            services = await bootstrap.initialize()

            assert isinstance(services, AppServices)
            assert isinstance(services.router, RouteExecutor)
            assert services.db_logger is None  # Disabled in mock_env
            assert services.routing_manager is None
            assert services.model_visibility_resolver is None
            assert services.model_router_registry is not None
            assert (
                services.model_router_registry._dependencies.health_registry
                is services.router._health_registry
            )
            assert registry_factory.call_args.kwargs["shared_fixed_router"] is services.router
            assert services.model_router_registry._shared_fixed is services.router

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
            patch("serving.servers.bootstrap.ResponseStore", return_value=AsyncMock()),
            patch("serving.servers.bootstrap.AgentJobStore", return_value=AsyncMock()),
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
            patch("serving.servers.bootstrap.ResponseStore", return_value=AsyncMock()),
            patch("serving.servers.bootstrap.AgentJobStore", return_value=AsyncMock()),
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
            patch("serving.servers.bootstrap.ResponseStore", return_value=AsyncMock()),
            patch("serving.servers.bootstrap.AgentJobStore", return_value=AsyncMock()),
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
    async def test_initialize_aliases_share_routewise_router_instance(
        self, mock_env, tmp_path, monkeypatch
    ):
        """Regression: an aliased request to a RouteWise model must resolve to
        the SAME stateful router instance as its canonical id.

        ``initialize`` has to pass ``alias_to_model`` into ``ModelRouterRegistry``.
        Without it the registry cannot collapse the alias onto the canonical
        model, so ``get_router(alias)`` builds a *separate*, un-bootstrapped
        RouteWise router (split session/latency state, no donor overrides or log
        warmup) instead of sharing the canonical one.
        """
        from routing.routewise.router import RouteWiseRouter

        models_yaml = tmp_path / "rw_models.yaml"
        models_yaml.write_text(
            """
models:
  - id: rw-model
    name: RW Model
    provider: vllm
    base_url: ${LOCAL_DEPLOYMENT_URL}
    context_length: 8192
    max_output_length: 4096
    router: routewise
    aliases: ["rw-alias"]
    route:
      - kind: vllm
        weight: 1.0
        base_url: ${LOCAL_DEPLOYMENT_URL}
"""
        )
        monkeypatch.setenv("MODELS_CONFIG", str(models_yaml))
        monkeypatch.setenv("LOCAL_DEPLOYMENT_URL", "http://localhost:8001")

        with (
            patch("serving.servers.bootstrap._init_db_logger", return_value=None),
            patch("serving.servers.bootstrap._apply_routing_manager", return_value=None),
            # Avoid spawning RouteWise background tasks (sweep / quota refresh).
            patch.object(RouteWiseRouter, "start", new=AsyncMock()),
        ):
            services = await bootstrap.initialize()

        reg = services.model_router_registry
        assert reg is not None
        canonical = reg.get_router("rw-model")
        alias = reg.get_router("rw-alias")
        assert isinstance(canonical, RouteWiseRouter)
        # The alias must not split off a second RouteWise instance.
        assert alias is canonical
        assert alias.concurrency_pools is canonical.concurrency_pools
        # Startup composes Fixed and RouteWise around one process-scoped
        # registry so circuit/availability state cannot split by strategy.
        assert canonical._health_registry is services.router._health_registry

    @pytest.mark.asyncio
    async def test_initialize_constructs_user_concurrency_limiter(self, mock_env):
        """services.user_concurrency_limiter must be a UserConcurrencyLimiter
        with caps for free/pro/internal/admin."""
        from serving.servers.concurrency import UNLIMITED_CONCURRENCY, UserConcurrencyLimiter

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
            for role in ("trial", "free", "pro", "internal"):
                granted, cap, _ = await limiter.try_acquire(f"u-{role}", role, False)
                assert granted
                assert cap >= 1
            # is_admin=True must yield the admin cap: the unlimited sentinel (0)
            granted, cap, label = await limiter.try_acquire("admin-user", "free", True)
            assert granted
            assert cap == UNLIMITED_CONCURRENCY
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
        mock_routewise = _mock_routewise()
        mock_routewise.start = AsyncMock()

        info = MagicMock()
        info.model_id = "m"
        info.aliases = []
        info.router = "routewise"
        info.strategy = None
        info.router_params = None

        # A spec-backed instance keeps the production isinstance boundary honest.
        registry_instance = MagicMock()
        registry_instance.get_router = MagicMock(return_value=mock_routewise)
        registry_instance.get_router_name.return_value = "routewise"
        registry_instance.managed_routers.return_value = []
        registry_instance.runtime_override_for_router.return_value = None

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
    async def test_initialize_fails_when_routewise_returns_same_named_object(self, mock_env):
        """A declared strategy cannot satisfy RouteWise using only its class name."""
        same_named_router = type("RouteWiseRouter", (), {})()
        info = ModelRegistrationInfo(model_id="m", router="routewise")
        registry_instance = MagicMock()
        registry_instance.get_router.return_value = same_named_router
        registry_instance.get_router_name.return_value = "routewise"
        registry_instance.managed_routers.return_value = []

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
            pytest.raises(
                TypeError,
                match="routewise strategy returned RouteWiseRouter for model 'm'",
            ),
        ):
            await bootstrap.initialize()

    def test_collect_restored_routewise_rejects_same_named_object(self):
        """The dedicated type error lets DB restore escape its best-effort catch."""
        same_named_router = type("RouteWiseRouter", (), {})()
        registry_instance = MagicMock()
        registry_instance.get_router.return_value = same_named_router

        with pytest.raises(
            bootstrap._RouteWiseRouterTypeError,
            match="restored routewise strategy returned RouteWiseRouter for model 'runtime-m'",
        ):
            bootstrap._collect_routewise_runtime_routers(
                registry_instance,
                {"runtime-m"},
                [],
            )

    @pytest.mark.asyncio
    async def test_initialize_bootstraps_restored_runtime_routewise_before_start(self, mock_env):
        """Runtime-restored RouteWise models should replay DB logs before start()."""
        events: list[str] = []
        runtime_routewise = _mock_routewise()
        runtime_routewise.start = AsyncMock(side_effect=lambda: events.append("start"))
        runtime_routewise.bootstrap_from_probe_rows.return_value = {
            "rows": 0,
            "latency_events": 0,
            "latency_prior_samples": 0,
        }

        registry_instance = MagicMock()
        registry_instance.get_router = MagicMock(return_value=runtime_routewise)
        registry_instance.managed_routers.return_value = []
        registry_instance.configured_model_ids.return_value = []
        registry_instance.registered_models.return_value = {"runtime-m": "RouteWiseRouter"}
        registry_instance.canonical_model_id.side_effect = lambda model_id: model_id
        registry_instance.has_model.return_value = True
        registry_instance.get_configured_routewise_params.return_value = {}
        registry_instance.get_cached_router.return_value = runtime_routewise

        mock_db_logger = AsyncMock()
        mock_db_logger.pool = object()
        pg_store = AsyncMock()
        cached_store = MagicMock()
        cached_store.list_settings = AsyncMock(return_value=[])
        cached_store.get_setting = AsyncMock(return_value=None)
        cached_store.list_routewise_probe_samples = AsyncMock(return_value=[])
        log_store = MagicMock()
        runtime_routewise.apply_runtime_overrides.side_effect = lambda **_kwargs: events.append(
            "settings"
        )

        async def record_routewise_bootstrap(
            _log_store, routewise_routers, model_ids_by_router, *_
        ):
            if routewise_routers:
                events.append(
                    "bootstrap:"
                    + ",".join(sorted(model_ids_by_router.get(id(routewise_routers[0]), set())))
                )

        async def record_provider_route_config_restore(*_args):
            events.append("configs")

        with (
            patch("serving.servers.bootstrap._init_db_logger", return_value=mock_db_logger),
            patch(
                "serving.servers.bootstrap._init_router_and_models",
                new=AsyncMock(return_value=({}, [])),
            ),
            patch("serving.servers.bootstrap._apply_routing_manager", return_value=None),
            patch(
                "serving.servers.bootstrap.ModelRouterRegistry",
                return_value=registry_instance,
            ),
            patch("serving.servers.bootstrap.PostgresOperationalStore", return_value=pg_store),
            patch("serving.servers.bootstrap.CachedOperationalStore", return_value=cached_store),
            patch("serving.servers.bootstrap.PostgresLogStore", return_value=log_store),
            patch("serving.servers.bootstrap.ResponseStore", return_value=AsyncMock()),
            patch("serving.servers.bootstrap.AgentJobStore", return_value=AsyncMock()),
            patch("serving.servers.bootstrap.email_scheduler.start_scheduler"),
            patch(
                "serving.servers.bootstrap.email_scheduler.rehydrate_scheduled_broadcasts",
                new=AsyncMock(),
            ),
            patch("serving.servers.bootstrap.email_scheduler.get_scheduler", return_value=None),
            patch("serving.adapters.dynamic_keys.apply_db_keys_at_boot", new=AsyncMock()),
            patch(
                "serving.servers.routers.admin.provider_routes."
                "apply_persisted_model_router_strategy_overrides",
                new=AsyncMock(),
            ),
            patch(
                "serving.servers.routers.admin.provider_routes."
                "apply_persisted_provider_route_candidates",
                new=AsyncMock(return_value={"runtime-m"}),
            ),
            patch(
                "serving.servers.routers.admin.provider_routes."
                "apply_persisted_provider_route_configs",
                new=AsyncMock(side_effect=record_provider_route_config_restore),
            ),
            patch(
                "serving.servers.bootstrap._bootstrap_routewise_from_logs",
                new=AsyncMock(side_effect=record_routewise_bootstrap),
            ) as bootstrap_logs,
        ):
            services = await bootstrap.initialize()

        assert services.managed_routers == [runtime_routewise]
        assert events == ["configs", "bootstrap:runtime-m", "settings", "start"]
        assert bootstrap_logs.await_count == 1
        runtime_call = bootstrap_logs.await_args_list[0]
        assert runtime_call.args[0] is log_store
        assert runtime_call.args[1] == [runtime_routewise]
        assert runtime_call.args[2] == {id(runtime_routewise): {"runtime-m"}}
        runtime_routewise.attach_operational_store.assert_called_once_with(cached_store)
        runtime_routewise.bootstrap_from_probe_rows.assert_called_once_with([])
        runtime_routewise.set_probe_sample_watermark.assert_called_once_with(0)

    @pytest.mark.asyncio
    async def test_routewise_db_bootstrap_replays_recent_logs(self):
        """RouteWise DB bootstrap queries assigned model ids and replays rows."""
        log_store = AsyncMock()
        log_store.get_routewise_bootstrap_rows.side_effect = [
            [{"model_id": "m", "source": "latency"}],
            [{"model_id": "m", "source": "envelope"}],
        ]
        rw = _mock_routewise(
            config=RouteWiseConfig(
                db_bootstrap_max_rows=123,
                latency_window_sec=900.0,
                latency_history_prior_window_sec=3600.0,
                envelope_window_hours=24,
            )
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
        rw = _mock_routewise(
            config=RouteWiseConfig(
                db_bootstrap_max_rows=123,
                latency_window_sec=900.0,
                envelope_window_hours=24,
            )
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
        rw = _mock_routewise(
            config=RouteWiseConfig(
                db_bootstrap_max_rows=123,
                latency_window_sec=900.0,
                envelope_window_hours=24,
            )
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
        rw = _mock_routewise(
            config=RouteWiseConfig(
                db_bootstrap_max_rows=123,
                latency_window_sec=900.0,
                envelope_window_hours=24,
            )
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
    async def test_probe_bootstrap_reports_missing_required_watermark_method(self):
        """A broken typed router must warn instead of silently skipping its watermark."""
        store = AsyncMock()
        store.list_routewise_probe_samples.return_value = [
            {
                "id": 7,
                "model_id": "m",
                "checked_at": None,
            }
        ]
        rw = _mock_routewise(config=RouteWiseConfig(db_bootstrap_max_rows=10))
        rw.bootstrap_from_probe_rows.return_value = {
            "rows": 1,
            "latency_events": 1,
            "latency_prior_samples": 0,
        }
        rw.set_probe_sample_watermark = None

        with patch("serving.servers.bootstrap.logger") as mock_logger:
            await bootstrap._bootstrap_routewise_from_probe_samples(
                store,
                [rw],
                {id(rw): {"m"}},
            )

        rw.bootstrap_from_probe_rows.assert_called_once()
        mock_logger.warning.assert_called_once_with(
            "RouteWise probe bootstrap failed for models %s",
            ["m"],
            exc_info=True,
        )

    @pytest.mark.asyncio
    async def test_envelope_not_calibrated_aborts_initialization(self, mock_env):
        """An uncalibrated quota envelope must fail boot, not silently start."""
        from routing.routewise.envelope import EnvelopeNotCalibratedError

        mock_routewise = _mock_routewise()
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
        registry_instance.get_router = MagicMock(return_value=mock_routewise)
        registry_instance.get_router_name.return_value = "routewise"
        registry_instance.managed_routers.return_value = []
        registry_instance.runtime_override_for_router.return_value = None

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
    async def test_failed_runtime_router_override_does_not_fall_back_to_fixed(self, mock_env):
        """A resource-aware runtime override must fail closed at startup."""
        from routing.routewise.envelope import EnvelopeNotCalibratedError

        managed_router = _mock_routewise()
        managed_router.start = AsyncMock(
            side_effect=EnvelopeNotCalibratedError("envelope is uncalibrated")
        )

        info = MagicMock()
        info.model_id = "m"
        info.aliases = []
        info.router = "routewise"
        info.strategy = None
        info.router_params = None

        override = SimpleNamespace(canonical_model_id="m", configured_strategy="fixed")
        registry = MagicMock()
        registry.get_router = MagicMock(return_value=managed_router)
        registry.get_router_name.return_value = "routewise"
        registry.managed_routers.return_value = []
        registry.runtime_override_for_router.return_value = override

        with (
            patch("serving.servers.bootstrap._init_db_logger", return_value=None),
            patch(
                "serving.servers.bootstrap._init_router_and_models",
                new=AsyncMock(return_value=({}, [info])),
            ),
            patch("serving.servers.bootstrap._apply_routing_manager", return_value=None),
            patch("serving.servers.bootstrap.ModelRouterRegistry", return_value=registry),
            patch(
                "serving.servers.bootstrap._bootstrap_routewise_from_logs",
                new=AsyncMock(),
            ),
            patch("serving.servers.bootstrap.logger") as mock_logger,
            pytest.raises(EnvelopeNotCalibratedError, match="uncalibrated"),
        ):
            await bootstrap.initialize()

        registry.runtime_override_for_router.assert_called_once_with(managed_router)
        registry.prepare_router_strategy_change.assert_not_called()
        registry.commit_router_strategy_change.assert_not_called()
        mock_logger.error.assert_called_once_with(
            "Runtime RouteWise override failed envelope calibration for "
            "model=%s; refusing unsafe fallback to strategy=%s",
            "m",
            "fixed",
        )


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
