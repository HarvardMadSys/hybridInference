"""Unit tests for ModelRouterRegistry (config-driven dispatch)."""

from __future__ import annotations

import logging

import pytest


@pytest.mark.unit
class TestModelRouterRegistry:
    def test_propagates_dependencies_and_rejects_mismatched_fixed_router(self):
        from routing.dependencies import RouterBuildDependencies
        from routing.endpoint_health import EndpointHealthRegistry
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter

        health_registry = EndpointHealthRegistry()
        dependencies = RouterBuildDependencies(health_registry=health_registry)
        shared_fixed = FixedRouter(health_registry=health_registry)
        with pytest.raises(ValueError, match="must use RouterBuildDependencies"):
            ModelRouterRegistry(
                models_config={"rw": {"router": "routewise"}},
                dependencies=dependencies,
                shared_fixed_router=FixedRouter(),
            )

        reg = ModelRouterRegistry(
            models_config={"rw": {"router": "routewise"}},
            dependencies=dependencies,
            shared_fixed_router=shared_fixed,
        )
        routewise = reg.get_router("rw")

        assert routewise._health_registry is health_registry
        assert routewise._health_registry is shared_fixed._health_registry

    def test_validate_router_strategy_propagates_dependencies(self, monkeypatch):
        import routing.model_router_registry as registry_module
        from routing.dependencies import RouterBuildDependencies
        from routing.endpoint_health import EndpointHealthRegistry
        from routing.model_router_registry import ModelRouterRegistry

        dependencies = RouterBuildDependencies(
            health_registry=EndpointHealthRegistry(),
        )
        received_dependencies = []

        def _validate_router_config(name, params, *, dependencies=None):
            received_dependencies.append(dependencies)

        monkeypatch.setattr(
            registry_module,
            "validate_router_config",
            _validate_router_config,
        )
        reg = ModelRouterRegistry(models_config={"model": {}}, dependencies=dependencies)

        reg.validate_router_strategy("model", "fixed")

        assert received_dependencies == [dependencies]

    def test_fixed_router_validation_does_not_build_throwaway(self, monkeypatch):
        from unittest.mock import MagicMock

        import routing.model_router_registry as registry_module
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter

        validate = MagicMock()
        build = MagicMock(side_effect=AssertionError("fixed router must not be built"))
        monkeypatch.setattr(registry_module, "validate_router_config", validate)
        monkeypatch.setattr(registry_module, "build_router", build)
        shared_fixed = FixedRouter()
        reg = ModelRouterRegistry(
            models_config={"model": {"router": "fixed"}},
            shared_fixed_router=shared_fixed,
        )

        assert reg.get_router("model") is shared_fixed
        validate.assert_called_once_with("fixed", {}, dependencies=None)
        build.assert_not_called()

    def test_get_before_bind_is_not_cached_and_recovers_after_compat_bind(self):
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter

        reg = ModelRouterRegistry(models_config={"model": {"router": "fixed"}})

        with pytest.raises(RuntimeError, match="shared FixedRouter is not registered"):
            reg.get_router("model")
        assert reg.registered_models() == {}

        shared_fixed = FixedRouter()
        reg.bind_fixed_router(shared_fixed)

        assert reg.get_router("model") is shared_fixed

    def test_get_before_bind_reports_invalid_params_before_missing_shared_router(self):
        from pydantic import ValidationError

        from routing.model_router_registry import ModelRouterRegistry

        reg = ModelRouterRegistry(
            models_config={
                "model": {
                    "router": "fixed",
                    "router_params": {"unknown_key": 1},
                }
            }
        )

        with pytest.raises(ValidationError):
            reg.get_router("model")
        assert reg.registered_models() == {}

    def test_compat_bind_is_idempotent_but_rejects_a_different_router(self):
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter

        shared_fixed = FixedRouter()
        reg = ModelRouterRegistry(
            models_config={"model": {}},
            shared_fixed_router=shared_fixed,
        )

        reg.bind_fixed_router(shared_fixed)
        with pytest.raises(ValueError, match="different shared FixedRouter"):
            reg.bind_fixed_router(FixedRouter())

        assert reg.get_router("model") is shared_fixed

    def test_compat_bind_rejects_registration_after_cache_is_populated(self):
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter

        reg = ModelRouterRegistry(models_config={"model": {}})
        reg._cache["model"] = FixedRouter()

        with pytest.raises(RuntimeError, match="before router lookup"):
            reg.bind_fixed_router(FixedRouter())

    def test_failed_override_construction_preserves_previous_router_and_state(self):
        from pydantic import BaseModel

        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter
        from routing.strategies import _STRATEGIES, register_strategy

        class _Params(BaseModel):
            model_config = {"extra": "forbid"}

        class _FailingRouter:
            def __init__(self, params=None):
                raise RuntimeError("constructor failed")

        strategy = "__test_failing_override__"
        register_strategy(strategy)((_FailingRouter, _Params))
        shared_fixed = FixedRouter()
        reg = ModelRouterRegistry(
            models_config={"model": {"router": "fixed"}},
            shared_fixed_router=shared_fixed,
        )
        previous = reg.get_router("model")

        try:
            with pytest.raises(RuntimeError, match="constructor failed"):
                reg.set_router_override("model", strategy)

            assert reg.get_router_override("model") is None
            assert reg.get_router_name("model") == "fixed"
            assert reg.get_router("model") is previous
            assert previous is shared_fixed
        finally:
            _STRATEGIES.pop(strategy, None)

    def test_successful_override_constructs_and_attaches_candidate_once(self):
        from pydantic import BaseModel

        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter
        from routing.strategies import _STRATEGIES, register_strategy

        class _Params(BaseModel):
            model_config = {"extra": "forbid"}

        class _CountingRouter:
            constructor_calls = 0
            attach_calls = 0

            def __init__(self, params=None):
                type(self).constructor_calls += 1

            def attach_route_table(self, route_table):
                type(self).attach_calls += 1
                self.route_table = route_table

        strategy = "__test_counting_override__"
        register_strategy(strategy)((_CountingRouter, _Params))
        shared_fixed = FixedRouter()
        reg = ModelRouterRegistry(
            models_config={"model": {"router": "fixed"}},
            shared_fixed_router=shared_fixed,
        )

        try:
            reg.set_router_override("model", strategy)
            first = reg.get_router("model")
            second = reg.get_router("model")

            assert first is second
            assert first.route_table is shared_fixed
            assert _CountingRouter.constructor_calls == 1
            assert _CountingRouter.attach_calls == 1
        finally:
            _STRATEGIES.pop(strategy, None)

    def test_runtime_rebuilt_routewise_router_keeps_shared_health_state(self):
        from routing.dependencies import RouterBuildDependencies
        from routing.endpoint_health import EndpointHealthRegistry
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter

        health_registry = EndpointHealthRegistry()
        dependencies = RouterBuildDependencies(health_registry=health_registry)
        fixed = FixedRouter(health_registry=health_registry)
        reg = ModelRouterRegistry(
            models_config={"model": {"router": "routewise"}},
            dependencies=dependencies,
            shared_fixed_router=fixed,
        )

        first_routewise = reg.get_router("model")
        health_registry.record_success("shared:endpoint")

        reg.set_router_override("model", "fixed")
        assert reg.get_router("model") is fixed

        reg.set_router_override("model", "routewise")
        rebuilt_routewise = reg.get_router("model")

        assert rebuilt_routewise is not first_routewise
        assert rebuilt_routewise._health_registry is health_registry
        assert "shared:endpoint" in rebuilt_routewise.get_provider_status()

    def test_get_router_returns_fixed_for_unspecified_model(self):
        """Model without 'router:' falls back to default_router_name and
        returns the bound shared FixedRouter instance (not a fresh one)."""
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter

        shared_fixed = FixedRouter()
        models_config = {"glm-4.7": {}}
        reg = ModelRouterRegistry(
            models_config=models_config,
            default_router_name="fixed",
            shared_fixed_router=shared_fixed,
        )
        router = reg.get_router("glm-4.7")
        assert isinstance(router, FixedRouter)
        # CRITICAL: must be the same instance as the bound shared FixedRouter,
        # not a fresh one with an empty routes dict.
        assert router is shared_fixed

    def test_get_router_returns_routewise_when_specified(self):
        """Model with 'router: routewise' returns a RouteWiseRouter instance."""
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter
        from routing.routewise.router import RouteWiseRouter

        models_config = {"glm-4.7": {"router": "routewise"}}
        reg = ModelRouterRegistry(
            models_config=models_config,
            default_router_name="fixed",
            shared_fixed_router=FixedRouter(),
        )
        router = reg.get_router("glm-4.7")
        assert isinstance(router, RouteWiseRouter)

    def test_get_router_caches_per_model(self):
        """Repeated get_router(same_model) returns the same instance."""
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter

        shared_fixed = FixedRouter()
        reg = ModelRouterRegistry(
            models_config={"glm-4.7": {}},
            default_router_name="fixed",
            shared_fixed_router=shared_fixed,
        )
        a = reg.get_router("glm-4.7")
        b = reg.get_router("glm-4.7")
        assert a is b

    def test_get_router_alias_returns_canonical_routewise_instance(self):
        """Aliases must not split RouteWise stateful router instances."""
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter
        from routing.routewise.router import RouteWiseRouter

        reg = ModelRouterRegistry(
            models_config={
                "minimax-m2.5": {
                    "router": "routewise",
                    "router_params": {
                        "budget_alpha": 0.5,
                    },
                },
                "MiniMax-M2.5": {
                    "router": "routewise",
                    "router_params": {
                        "budget_alpha": 0.5,
                    },
                },
            },
            default_router_name="fixed",
            alias_to_model={"MiniMax-M2.5": "minimax-m2.5"},
            shared_fixed_router=FixedRouter(),
        )

        canonical = reg.get_router("minimax-m2.5")
        alias = reg.get_router("MiniMax-M2.5")

        assert isinstance(canonical, RouteWiseRouter)
        assert alias is canonical
        assert alias.concurrency_pools is canonical.concurrency_pools

    def test_get_router_emits_router_initialized_log(self, caplog):
        """Cache miss logs a router_initialized event."""
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter
        from serving.utils.logging import JsonFormatter

        shared_fixed = FixedRouter()
        reg = ModelRouterRegistry(
            models_config={"glm-4.7": {"router_params": {"local_fraction": 0.7}}},
            default_router_name="fixed",
            shared_fixed_router=shared_fixed,
        )
        with caplog.at_level(logging.INFO, logger="routing.model_router_registry"):
            reg.get_router("glm-4.7")
        events = [r for r in caplog.records if getattr(r, "event", None) == "router_initialized"]
        assert len(events) == 1
        rec = events[0]
        assert rec.model == "glm-4.7"
        assert rec.strategy == "fixed"
        assert rec.param_keys == ["local_fraction"]

        payload = JsonFormatter().format(rec)
        assert '"event": "router_initialized"' in payload
        assert '"strategy": "fixed"' in payload
        assert '"param_keys": ["local_fraction"]' in payload

    def test_get_router_uses_default_router_from_config(self):
        """default_router_name='routewise' applies when model omits 'router'."""
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter
        from routing.routewise.router import RouteWiseRouter

        shared_fixed = FixedRouter()
        reg = ModelRouterRegistry(
            models_config={"glm-4.7": {}},
            default_router_name="routewise",
            shared_fixed_router=shared_fixed,
        )
        router = reg.get_router("glm-4.7")
        assert isinstance(router, RouteWiseRouter)

    def test_configured_router_name_ignores_runtime_override(self):
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter

        shared_fixed = FixedRouter()
        reg = ModelRouterRegistry(
            models_config={"glm-4.7": {"router": "fixed"}},
            default_router_name="fixed",
            shared_fixed_router=shared_fixed,
        )

        reg.set_router_override("glm-4.7", "routewise")

        assert reg.get_router_name("glm-4.7") == "routewise"
        assert reg.get_configured_router_name("glm-4.7") == "fixed"

    def test_clear_router_override_restores_configured_strategy(self):
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter

        fixed_router = FixedRouter()
        reg = ModelRouterRegistry(
            models_config={"glm-4.7": {"router": "fixed"}},
            default_router_name="fixed",
            shared_fixed_router=fixed_router,
        )

        reg.set_router_override("glm-4.7", "routewise")
        routewise_router = reg.get_router("glm-4.7")
        reg.clear_router_override("glm-4.7")

        assert reg.get_router_override("glm-4.7") is None
        assert reg.get_router_name("glm-4.7") == "fixed"
        assert reg.get_router("glm-4.7") is fixed_router
        assert reg.get_router("glm-4.7") is not routewise_router

    def test_get_router_unknown_strategy_raises(self):
        from routing.model_router_registry import ModelRouterRegistry

        reg = ModelRouterRegistry(
            models_config={"x": {"router": "made-up-strategy"}},
            default_router_name="fixed",
        )
        with pytest.raises(ValueError) as exc:
            reg.get_router("x")
        assert "made-up-strategy" in str(exc.value)

    def test_registered_models_listing(self):
        """registered_models() returns class names of cached routers."""
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter

        shared_fixed = FixedRouter()
        reg = ModelRouterRegistry(
            models_config={"a": {}, "b": {}},
            default_router_name="fixed",
            shared_fixed_router=shared_fixed,
        )
        reg.get_router("a")
        reg.get_router("b")
        mapping = reg.registered_models()
        assert mapping == {"a": "FixedRouter", "b": "FixedRouter"}

    def test_get_router_fixed_returns_shared_instance_with_routes(self):
        """The 'fixed' strategy must return the bound shared FixedRouter,
        and it must already have the model's routes registered (regression
        for Copilot review on PR #413: build_router('fixed', ...)
        previously returned a fresh empty FixedRouter, causing
        'No route configured for model' at request time)."""
        from unittest.mock import MagicMock

        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter

        # Build a shared FixedRouter and pre-register a route on it the way
        # bootstrap.initialize() does via register_from_models_yaml.
        shared_fixed = FixedRouter()
        adapter = MagicMock()
        adapter.config.provider = "openai_compat"
        adapter.config.endpoint_id = "openai_compat"
        shared_fixed.register_route("glm-4.7", [(adapter, 1.0)])

        reg = ModelRouterRegistry(
            models_config={"glm-4.7": {"router": "fixed"}},
            default_router_name="fixed",
            shared_fixed_router=shared_fixed,
        )

        router = reg.get_router("glm-4.7")
        # Same instance as the bound shared FixedRouter.
        assert router is shared_fixed
        # Routes registered on the shared instance are visible.
        assert "glm-4.7" in router.routes

    def test_managed_routers_returns_unique_start_stop_capable_routers(self):
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter

        shared_fixed = FixedRouter()
        reg = ModelRouterRegistry(
            models_config={
                "glm-4.7": {"router": "routewise"},
                "glm-4.7-alias": {"router": "routewise"},
                "fixed-model": {},
            },
            default_router_name="fixed",
            shared_fixed_router=shared_fixed,
        )
        first = reg.get_router("glm-4.7")
        second = reg.get_router("glm-4.7-alias")
        reg.get_router("fixed-model")

        managed = reg.managed_routers()

        assert first in managed
        assert second in managed
        assert all(hasattr(router, "start") and hasattr(router, "stop") for router in managed)
        assert len({id(router) for router in managed}) == len(managed)

    def test_refresh_route_tables_deduplicates_aliases_and_uses_public_capability(self):
        from routing.model_router_registry import ModelRouterRegistry

        class _RefreshableRouter:
            def __init__(self) -> None:
                self.refresh_calls = 0
                self.legacy_calls = 0

            def refresh_route_table(self) -> None:
                self.refresh_calls += 1

            def _rebuild_from_route_table(self) -> None:
                self.legacy_calls += 1

        router = _RefreshableRouter()
        reg = ModelRouterRegistry(models_config={})
        reg._cache.update({"canonical": router, "alias": router})

        reg.refresh_route_tables()

        assert router.refresh_calls == 1
        assert router.legacy_calls == 0

    def test_refresh_route_tables_runs_legacy_fallback_under_router_lock(self):
        from routing.model_router_registry import ModelRouterRegistry

        class _RecordingLock:
            def __init__(self) -> None:
                self.depth = 0

            def __enter__(self) -> _RecordingLock:
                self.depth += 1
                return self

            def __exit__(self, *_exc_info: object) -> None:
                self.depth -= 1

        commit_lock = _RecordingLock()

        class _LegacyRouter:
            _route_commit_lock = commit_lock

            def __init__(self) -> None:
                self.refresh_calls = 0

            def _rebuild_from_fixed_router(self) -> None:
                assert commit_lock.depth == 1
                self.refresh_calls += 1

        router = _LegacyRouter()
        reg = ModelRouterRegistry(models_config={})
        reg._cache.update({"canonical": router, "alias": router})

        reg.refresh_route_tables()

        assert router.refresh_calls == 1
        assert commit_lock.depth == 0
