"""Unit tests for ModelRouterRegistry."""

from __future__ import annotations

import pytest

from routing.model_router_registry import ModelRouterRegistry


def _make_mock_router(name: str):
    """Create a mock router with a deterministic class name."""
    cls = type(name, (), {})
    return cls()


@pytest.mark.unit
class TestModelRouterRegistry:
    def test_default_router_returned(self):
        """Unknown model returns the default (Fixed) router."""
        fixed = _make_mock_router("FixedRouter")
        registry = ModelRouterRegistry(default_router=fixed)

        assert registry.get_router("unknown-model") is fixed

    def test_registered_model_returns_correct_router(self):
        """Registered model returns its specific router."""
        fixed = _make_mock_router("FixedRouter")
        nimbus = _make_mock_router("NimbusRouter")

        registry = ModelRouterRegistry(default_router=fixed)
        registry.register("glm-4.6", nimbus)

        assert registry.get_router("glm-4.6") is nimbus
        assert registry.get_router("other-model") is fixed

    def test_registered_models_listing(self):
        """registered_models() returns correct mapping."""
        fixed = _make_mock_router("FixedRouter")
        nimbus = _make_mock_router("NimbusRouter")

        registry = ModelRouterRegistry(default_router=fixed)
        registry.register("glm-4.6", nimbus)
        registry.register("qwen3-coder-30b", nimbus)

        mapping = registry.registered_models()
        assert mapping == {
            "glm-4.6": "NimbusRouter",
            "qwen3-coder-30b": "NimbusRouter",
        }

    def test_alias_routing(self):
        """Aliases registered with the same router are resolved correctly."""
        fixed = _make_mock_router("FixedRouter")
        nimbus = _make_mock_router("NimbusRouter")

        registry = ModelRouterRegistry(default_router=fixed)
        # Register primary and alias with same router
        registry.register("glm-4.6", nimbus)
        registry.register("glm4.6", nimbus)

        assert registry.get_router("glm-4.6") is nimbus
        assert registry.get_router("glm4.6") is nimbus
        assert registry.get_router("unregistered") is fixed

    def test_override_registration(self):
        """Re-registering a model overwrites the previous router."""
        fixed = _make_mock_router("FixedRouter")
        nimbus = _make_mock_router("NimbusRouter")
        routewise = _make_mock_router("RouteWiseRouter")

        registry = ModelRouterRegistry(default_router=fixed)
        registry.register("glm-4.6", nimbus)
        assert registry.get_router("glm-4.6") is nimbus

        registry.register("glm-4.6", routewise)
        assert registry.get_router("glm-4.6") is routewise
