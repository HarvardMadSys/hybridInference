"""Unit tests for ModelRouterRegistry (config-driven dispatch)."""

from __future__ import annotations

import logging

import pytest


@pytest.mark.unit
class TestModelRouterRegistry:
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
        )
        reg.bind_fixed_router(shared_fixed)
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
        )
        # Bind a FixedRouter so RouteWise classification (via
        # attach_fixed_router) finds an empty routes dict and succeeds.
        reg.bind_fixed_router(FixedRouter())
        router = reg.get_router("glm-4.7")
        assert isinstance(router, RouteWiseRouter)

    def test_get_router_caches_per_model(self):
        """Repeated get_router(same_model) returns the same instance."""
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter

        reg = ModelRouterRegistry(
            models_config={"glm-4.7": {}},
            default_router_name="fixed",
        )
        reg.bind_fixed_router(FixedRouter())
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
        )
        reg.bind_fixed_router(FixedRouter())

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

        reg = ModelRouterRegistry(
            models_config={"glm-4.7": {"router_params": {"local_fraction": 0.7}}},
            default_router_name="fixed",
        )
        reg.bind_fixed_router(FixedRouter())
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

        reg = ModelRouterRegistry(
            models_config={"glm-4.7": {}},
            default_router_name="routewise",
        )
        reg.bind_fixed_router(FixedRouter())
        router = reg.get_router("glm-4.7")
        assert isinstance(router, RouteWiseRouter)

    def test_configured_router_name_ignores_runtime_override(self):
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter

        reg = ModelRouterRegistry(
            models_config={"glm-4.7": {"router": "fixed"}},
            default_router_name="fixed",
        )
        reg.bind_fixed_router(FixedRouter())

        reg.set_router_override("glm-4.7", "routewise")

        assert reg.get_router_name("glm-4.7") == "routewise"
        assert reg.get_configured_router_name("glm-4.7") == "fixed"

    def test_clear_router_override_restores_configured_strategy(self):
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter

        reg = ModelRouterRegistry(
            models_config={"glm-4.7": {"router": "fixed"}},
            default_router_name="fixed",
        )
        fixed_router = FixedRouter()
        reg.bind_fixed_router(fixed_router)

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

        reg = ModelRouterRegistry(
            models_config={"a": {}, "b": {}},
            default_router_name="fixed",
        )
        reg.bind_fixed_router(FixedRouter())
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
        )
        reg.bind_fixed_router(shared_fixed)

        router = reg.get_router("glm-4.7")
        # Same instance as the bound shared FixedRouter.
        assert router is shared_fixed
        # Routes registered on the shared instance are visible.
        assert "glm-4.7" in router.routes

    def test_managed_routers_returns_unique_start_stop_capable_routers(self):
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter

        reg = ModelRouterRegistry(
            models_config={
                "glm-4.7": {"router": "routewise"},
                "glm-4.7-alias": {"router": "routewise"},
                "fixed-model": {},
            },
            default_router_name="fixed",
        )
        reg.bind_fixed_router(FixedRouter())
        first = reg.get_router("glm-4.7")
        second = reg.get_router("glm-4.7-alias")
        reg.get_router("fixed-model")

        managed = reg.managed_routers()

        assert first in managed
        assert second in managed
        assert all(hasattr(router, "start") and hasattr(router, "stop") for router in managed)
        assert len({id(router) for router in managed}) == len(managed)
