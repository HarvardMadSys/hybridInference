"""Tests for ModelRouterRegistry canary rollout gate."""

from __future__ import annotations

import random
from unittest.mock import MagicMock, patch

import pytest

from routing.model_router_registry import ModelRouterRegistry

_METRICS_MODULE = "serving.observability.metrics"


def _make_router(name: str = "MockRouter") -> MagicMock:
    router = MagicMock()
    router.__class__.__name__ = name
    return router


@pytest.mark.unit
class TestCanaryGate:
    """Verify canary dispatch logic in ModelRouterRegistry."""

    def test_canary_disabled_passthrough(self):
        """When canary is disabled, always returns the registered router."""
        default = _make_router("FixedRouter")
        experimental = _make_router("RouteWiseRouter")
        reg = ModelRouterRegistry(default_router=default)
        reg.register("model-a", experimental)

        # Canary not configured -- passthrough.
        for _ in range(100):
            assert reg.get_router("model-a") is experimental

    def test_canary_model_allowlist(self):
        """Only allowlisted models go through the canary gate."""
        default = _make_router("FixedRouter")
        rw = _make_router("RouteWiseRouter")
        reg = ModelRouterRegistry(default_router=default)
        reg.register("model-a", rw)
        reg.register("model-b", rw)

        reg.configure_canary(
            enabled=True,
            target_router=rw,
            enabled_models=["model-a"],
            traffic_fraction=1.0,
        )

        # model-a: allowlisted, fraction=1.0 -> always experimental.
        with patch(f"{_METRICS_MODULE}.ROUTEWISE_CANARY_DECISIONS"), \
             patch(f"{_METRICS_MODULE}.normalize_model_label", return_value="m"):
            assert reg.get_router("model-a") is rw
        # model-b: not in allowlist -> always default.
        assert reg.get_router("model-b") is default

    def test_canary_traffic_fraction_zero(self):
        """fraction=0.0 sends all traffic to default."""
        default = _make_router("FixedRouter")
        rw = _make_router("RouteWiseRouter")
        reg = ModelRouterRegistry(default_router=default)
        reg.register("model-a", rw)

        reg.configure_canary(
            enabled=True,
            target_router=rw,
            enabled_models=None,
            traffic_fraction=0.0,
        )

        with patch(f"{_METRICS_MODULE}.ROUTEWISE_CANARY_DECISIONS"), \
             patch(f"{_METRICS_MODULE}.normalize_model_label", return_value="m"):
            for _ in range(100):
                assert reg.get_router("model-a") is default

    def test_canary_traffic_fraction_partial(self):
        """~50% split with N=1000, tolerance of 10%."""
        default = _make_router("FixedRouter")
        rw = _make_router("RouteWiseRouter")
        reg = ModelRouterRegistry(default_router=default)
        reg.register("model-a", rw)

        reg.configure_canary(
            enabled=True,
            target_router=rw,
            enabled_models=None,
            traffic_fraction=0.5,
        )

        random.seed(42)
        n = 1000
        with patch(f"{_METRICS_MODULE}.ROUTEWISE_CANARY_DECISIONS"), \
             patch(f"{_METRICS_MODULE}.normalize_model_label", return_value="m"):
            experimental_count = sum(
                1 for _ in range(n) if reg.get_router("model-a") is rw
            )
        # Expect ~50% with tolerance.
        assert 400 <= experimental_count <= 600, (
            f"Expected ~500 experimental routes, got {experimental_count}"
        )

    def test_canary_does_not_affect_default_router(self):
        """Non-registered models always get the default, regardless of canary."""
        default = _make_router("FixedRouter")
        rw = _make_router("RouteWiseRouter")
        reg = ModelRouterRegistry(default_router=default)
        reg.register("model-a", rw)

        reg.configure_canary(
            enabled=True,
            target_router=rw,
            enabled_models=None,
            traffic_fraction=0.5,
        )

        # model-b is not registered -> always default.
        for _ in range(100):
            assert reg.get_router("model-b") is default

    def test_canary_does_not_affect_nimbus_router(self):
        """Nimbus-registered models are never subject to canary gate."""
        default = _make_router("FixedRouter")
        nimbus = _make_router("NimbusRouter")
        rw = _make_router("RouteWiseRouter")
        reg = ModelRouterRegistry(default_router=default)
        reg.register("nimbus-model", nimbus)
        reg.register("rw-model", rw)

        reg.configure_canary(
            enabled=True,
            target_router=rw,
            enabled_models=None,
            traffic_fraction=0.0,  # All canary traffic -> default.
        )

        # Nimbus model: never affected by canary, always returns nimbus.
        for _ in range(100):
            assert reg.get_router("nimbus-model") is nimbus

        # RouteWise model: fraction=0 -> all to default.
        with patch(f"{_METRICS_MODULE}.ROUTEWISE_CANARY_DECISIONS"), \
             patch(f"{_METRICS_MODULE}.normalize_model_label", return_value="m"):
            for _ in range(100):
                assert reg.get_router("rw-model") is default

    def test_canary_empty_model_list_means_none(self):
        """enabled_models=[] means no models participate (not 'all')."""
        default = _make_router("FixedRouter")
        rw = _make_router("RouteWiseRouter")
        reg = ModelRouterRegistry(default_router=default)
        reg.register("model-a", rw)

        reg.configure_canary(
            enabled=True,
            target_router=rw,
            enabled_models=[],
            traffic_fraction=1.0,
        )

        # Empty allowlist -> no models go through canary -> all to default.
        for _ in range(100):
            assert reg.get_router("model-a") is default

    def test_canary_emits_dedicated_counter(self):
        """Canary gate emits ROUTEWISE_CANARY_DECISIONS, not ROUTING_STRATEGY_SELECTED."""
        default = _make_router("FixedRouter")
        rw = _make_router("RouteWiseRouter")
        reg = ModelRouterRegistry(default_router=default)
        reg.register("model-a", rw)

        reg.configure_canary(
            enabled=True,
            target_router=rw,
            enabled_models=None,
            traffic_fraction=0.0,  # All -> default.
        )

        with patch(f"{_METRICS_MODULE}.ROUTEWISE_CANARY_DECISIONS") as mock_cd, \
             patch(f"{_METRICS_MODULE}.normalize_model_label", return_value="model-a"):
            reg.get_router("model-a")
            mock_cd.labels.assert_called_once_with(model="model-a", outcome="default")
