"""Tests for RouteWise router scaffold."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from routing.routers import RoutingObservation
from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseRouter, SubscriptionType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_model_config(
    model_id: str = "test-model",
    provider: str = "openai_compat",
    subscription_type: str = "api",
    prompt_price: str = "0.001",
    completion_price: str = "0.002",
    endpoint_id: str | None = None,
) -> MagicMock:
    """Create a mock ModelConfig."""
    cfg = MagicMock()
    cfg.id = model_id
    cfg.provider = provider
    cfg.subscription_type = subscription_type
    cfg.endpoint_id = endpoint_id or f"{model_id}:{provider}"
    cfg.pricing = {"prompt": prompt_price, "completion": completion_price}
    return cfg


def _make_adapter(
    model_id: str = "test-model",
    provider: str = "openai_compat",
    subscription_type: str = "api",
    prompt_price: str = "0.001",
    completion_price: str = "0.002",
) -> MagicMock:
    """Create a mock adapter with a mock ModelConfig."""
    adapter = MagicMock()
    adapter.config = _make_model_config(
        model_id=model_id,
        provider=provider,
        subscription_type=subscription_type,
        prompt_price=prompt_price,
        completion_price=completion_price,
    )
    return adapter


@dataclass
class _FakeRouteConfig:
    adapters: list[tuple[Any, float]]


class _FakeFixedRouter:
    """Minimal stand-in for FixedRouter with a `routes` dict."""

    def __init__(self) -> None:
        self.routes: dict[str, _FakeRouteConfig] = {}

    def add(
        self, model_id: str, adapters_with_weights: list[tuple[Any, float]]
    ) -> None:
        self.routes[model_id] = _FakeRouteConfig(adapters=adapters_with_weights)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRouteWiseRouterScaffold:

    def test_scaffold_selects_adapter(self):
        """Router returns an adapter for a registered model."""
        adapter = _make_adapter()
        fr = _FakeFixedRouter()
        fr.add("test-model", [(adapter, 1.0)])

        router = RouteWiseRouter(
            fixed_router=fr, config=RouteWiseConfig()
        )
        selected = router._select_adapter("test-model", {})
        assert selected is adapter

    def test_unregistered_model_raises(self):
        """Requesting an unknown model raises ValueError."""
        fr = _FakeFixedRouter()
        router = RouteWiseRouter(
            fixed_router=fr, config=RouteWiseConfig()
        )
        with pytest.raises(ValueError, match="no route"):
            router._select_adapter("nonexistent", {})

    def test_subscription_type_classification(self):
        """Adapters are correctly classified by their subscription_type."""
        quota_adapter = _make_adapter(subscription_type="quota")
        api_adapter = _make_adapter(subscription_type="api")

        fr = _FakeFixedRouter()
        fr.add("test-model", [(quota_adapter, 0.5), (api_adapter, 0.5)])

        router = RouteWiseRouter(
            fixed_router=fr, config=RouteWiseConfig()
        )
        entries = router.classified["test-model"]
        types = {s for _, _, s in entries}
        assert SubscriptionType.QUOTA in types
        assert SubscriptionType.API in types

    def test_scaffold_prefers_quota_adapter(self):
        """Scaffold strategy prefers quota adapters over API adapters."""
        quota_adapter = _make_adapter(
            subscription_type="quota", prompt_price="0.010"
        )
        api_adapter = _make_adapter(
            subscription_type="api", prompt_price="0.001"
        )

        fr = _FakeFixedRouter()
        fr.add("test-model", [(api_adapter, 0.5), (quota_adapter, 0.5)])

        router = RouteWiseRouter(
            fixed_router=fr, config=RouteWiseConfig()
        )
        selected = router._select_adapter("test-model", {})
        assert selected is quota_adapter

    def test_scaffold_selects_cheapest_api_by_total_cost(self):
        """Cheapest API uses prompt + completion price (ADR 4.6)."""
        # Provider A: cheap prompt but expensive completion -> total 0.012
        provider_a = _make_adapter(
            subscription_type="api", prompt_price="0.002", completion_price="0.010"
        )
        # Provider B: balanced pricing -> total 0.006
        provider_b = _make_adapter(
            subscription_type="api", prompt_price="0.003", completion_price="0.003"
        )

        fr = _FakeFixedRouter()
        fr.add("test-model", [(provider_a, 0.5), (provider_b, 0.5)])

        router = RouteWiseRouter(
            fixed_router=fr, config=RouteWiseConfig()
        )
        selected = router._select_adapter("test-model", {})
        assert selected is provider_b

    def test_fallback_excludes_failed(self):
        """Fallback list excludes the adapter that just failed."""
        a1 = _make_adapter(provider="provider_a")
        a2 = _make_adapter(provider="provider_b")

        fr = _FakeFixedRouter()
        fr.add("test-model", [(a1, 0.5), (a2, 0.5)])

        router = RouteWiseRouter(
            fixed_router=fr, config=RouteWiseConfig()
        )
        fallbacks = router._get_fallback_adapters("test-model", a1)
        assert a1 not in fallbacks
        assert a2 in fallbacks

    def test_fallback_empty_for_unknown_model(self):
        """Fallback returns empty list for an unregistered model."""
        fr = _FakeFixedRouter()
        router = RouteWiseRouter(
            fixed_router=fr, config=RouteWiseConfig()
        )
        assert router._get_fallback_adapters("nonexistent", MagicMock()) == []

    def test_record_observation_noop(self):
        """record_observation does not raise."""
        fr = _FakeFixedRouter()
        router = RouteWiseRouter(
            fixed_router=fr, config=RouteWiseConfig()
        )
        obs = RoutingObservation(
            model_id="test-model",
            endpoint_id="test-model:local",
            ttft_ms=50.0,
            total_latency_ms=200.0,
            token_count=100,
            success=True,
            quota_committed=0.0,
        )
        router.record_observation(obs)  # Should not raise.

    def test_unknown_subscription_type_defaults_to_api(self):
        """Unknown subscription_type value falls back to API."""
        adapter = _make_adapter(subscription_type="unknown_tier")
        fr = _FakeFixedRouter()
        fr.add("test-model", [(adapter, 1.0)])

        router = RouteWiseRouter(
            fixed_router=fr, config=RouteWiseConfig()
        )
        entries = router.classified["test-model"]
        assert entries[0][2] is SubscriptionType.API

    def test_concurrency_adapter_skipped_when_disabled(self):
        """S_C adapter is not selected when concurrency_enabled=False."""
        conc = _make_adapter(subscription_type="concurrency")
        fr = _FakeFixedRouter()
        fr.add("test-model", [(conc, 1.0)])

        config = RouteWiseConfig(concurrency_enabled=False)
        router = RouteWiseRouter(fixed_router=fr, config=config)
        selected = router._select_adapter("test-model", {})
        assert selected is None

    def test_concurrency_adapter_selected_when_enabled(self):
        """S_C adapter is returned when concurrency_enabled=True."""
        conc = _make_adapter(subscription_type="concurrency")
        fr = _FakeFixedRouter()
        fr.add("test-model", [(conc, 1.0)])

        config = RouteWiseConfig(concurrency_enabled=True)
        router = RouteWiseRouter(fixed_router=fr, config=config)
        selected = router._select_adapter("test-model", {})
        assert selected is conc

    def test_fallback_excludes_concurrency_when_disabled(self):
        """Fallback list omits S_C adapters when concurrency_enabled=False."""
        api = _make_adapter(subscription_type="api")
        conc = _make_adapter(subscription_type="concurrency")

        fr = _FakeFixedRouter()
        fr.add("test-model", [(api, 0.5), (conc, 0.5)])

        config = RouteWiseConfig(concurrency_enabled=False)
        router = RouteWiseRouter(fixed_router=fr, config=config)
        fallbacks = router._get_fallback_adapters("test-model", api)
        assert conc not in fallbacks

    def test_fallback_includes_concurrency_when_enabled(self):
        """Fallback list includes S_C adapters when concurrency_enabled=True."""
        api = _make_adapter(subscription_type="api")
        conc = _make_adapter(subscription_type="concurrency")

        fr = _FakeFixedRouter()
        fr.add("test-model", [(api, 0.5), (conc, 0.5)])

        config = RouteWiseConfig(concurrency_enabled=True)
        router = RouteWiseRouter(fixed_router=fr, config=config)
        fallbacks = router._get_fallback_adapters("test-model", api)
        assert conc in fallbacks
