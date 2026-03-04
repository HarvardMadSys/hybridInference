"""Tests for RouteWise router -- scaffold + PD / LA-PD decision logic."""

from __future__ import annotations

from dataclasses import dataclass
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
    endpoint_id: str | None = None,
) -> MagicMock:
    """Create a mock adapter with a mock ModelConfig."""
    adapter = MagicMock()
    adapter.config = _make_model_config(
        model_id=model_id,
        provider=provider,
        subscription_type=subscription_type,
        prompt_price=prompt_price,
        completion_price=completion_price,
        endpoint_id=endpoint_id,
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


def _make_router_with_quota_and_api(
    config: RouteWiseConfig | None = None,
    prompt_price: str = "3.0",
    completion_price: str = "15.0",
) -> tuple[RouteWiseRouter, MagicMock, MagicMock]:
    """Build a RouteWiseRouter with one S_Q and one S_A adapter.

    Prices are per-1M-token (matching models.yaml convention).

    Returns:
        (router, quota_adapter, api_adapter)
    """
    if config is None:
        config = RouteWiseConfig()
    quota_adapter = _make_adapter(
        subscription_type="quota",
        prompt_price=prompt_price,
        completion_price=completion_price,
        endpoint_id="test-model:quota-provider",
    )
    api_adapter = _make_adapter(
        subscription_type="api",
        prompt_price=prompt_price,
        completion_price=completion_price,
        endpoint_id="test-model:api-provider",
    )
    fr = _FakeFixedRouter()
    fr.add("test-model", [(quota_adapter, 0.5), (api_adapter, 0.5)])
    router = RouteWiseRouter(fixed_router=fr, config=config)
    return router, quota_adapter, api_adapter


# ---------------------------------------------------------------------------
# Scaffold tests (retained from PR-2)
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

    def test_pd_selects_quota_when_value_high(self):
        """PD selects S_Q when estimated API cost exceeds shadow price.

        With per-1M prices (3.0 prompt, 15.0 completion) and cold-start
        predictor (q50=500), v_t = 15/1M * 500 = 0.0075 > L_seed=0.001.
        """
        quota_adapter = _make_adapter(
            subscription_type="quota", prompt_price="3.0", completion_price="15.0",
            endpoint_id="test-model:quota-provider",
        )
        api_adapter = _make_adapter(
            subscription_type="api", prompt_price="3.0", completion_price="15.0",
            endpoint_id="test-model:api-provider",
        )

        fr = _FakeFixedRouter()
        fr.add("test-model", [(api_adapter, 0.5), (quota_adapter, 0.5)])

        router = RouteWiseRouter(
            fixed_router=fr, config=RouteWiseConfig()
        )
        selected = router._select_adapter("test-model", {})
        assert selected is quota_adapter

    def test_scaffold_selects_cheapest_api_by_request_cost(self):
        """Cheapest API is selected per-request, not by fixed unit sum.

        With cold-start prediction (q50=500) and 0 prompt tokens, the
        selection depends only on completion price.  Provider B (0.003/1M)
        beats Provider A (0.010/1M) on completion price.
        """
        # Provider A: cheap prompt but expensive completion
        provider_a = _make_adapter(
            subscription_type="api", prompt_price="0.002", completion_price="0.010"
        )
        # Provider B: balanced pricing
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

    def test_per_request_cheapest_varies_by_prompt_ratio(self):
        """The cheapest S_A adapter can change with prompt/output ratio.

        Provider A: prompt=0.5/1M, completion=20.0/1M  (cheap input)
        Provider B: prompt=5.0/1M, completion=5.0/1M   (balanced)

        With large prompt_tokens and small predicted output, A is cheaper.
        With small prompt_tokens and large predicted output, B is cheaper.
        """
        provider_a = _make_adapter(
            subscription_type="api", prompt_price="0.5", completion_price="20.0",
        )
        provider_b = _make_adapter(
            subscription_type="api", prompt_price="5.0", completion_price="5.0",
        )

        fr = _FakeFixedRouter()
        fr.add("test-model", [(provider_a, 0.5), (provider_b, 0.5)])

        router = RouteWiseRouter(fixed_router=fr, config=RouteWiseConfig())

        # Warm predictor so predicted output is ~100 (small output).
        for _ in range(25):
            router.predictor.update("test-model", 100)

        # Large prompt (10000), small output (~100).
        # A: 0.5/1M * 10000 + 20.0/1M * 100 = 0.005 + 0.002 = 0.007
        # B: 5.0/1M * 10000 + 5.0/1M * 100  = 0.050 + 0.0005 = 0.0505
        # A is cheaper.
        selected = router._select_adapter("test-model", {"prompt_tokens": 10000})
        assert selected is provider_a

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

    def test_fallback_excludes_concurrency(self):
        """Fallback never includes S_C adapters (regardless of feature flag).

        S_C is excluded from fallback because the BaseRouter fallback path
        bypasses _select_adapter -- no concurrency slot accounting would occur.
        """
        api = _make_adapter(subscription_type="api")
        conc = _make_adapter(subscription_type="concurrency")

        fr = _FakeFixedRouter()
        fr.add("test-model", [(api, 0.5), (conc, 0.5)])

        # Excluded when disabled.
        config = RouteWiseConfig(concurrency_enabled=False)
        router = RouteWiseRouter(fixed_router=fr, config=config)
        assert conc not in router._get_fallback_adapters("test-model", api)

        # Still excluded when enabled.
        config = RouteWiseConfig(concurrency_enabled=True)
        router = RouteWiseRouter(fixed_router=fr, config=config)
        assert conc not in router._get_fallback_adapters("test-model", api)

    def test_fallback_only_includes_api_adapters(self):
        """Fallback only returns S_A adapters, never S_Q or S_C.

        The BaseRouter fallback path bypasses _select_adapter entirely,
        so no PD decision or quota/concurrency accounting is performed.
        Only S_A is safe for fallback.
        """
        quota = _make_adapter(subscription_type="quota")
        conc = _make_adapter(subscription_type="concurrency")
        api_a = _make_adapter(subscription_type="api", provider="provider_a")
        api_b = _make_adapter(subscription_type="api", provider="provider_b")

        fr = _FakeFixedRouter()
        fr.add("test-model", [
            (quota, 0.2), (conc, 0.2), (api_a, 0.3), (api_b, 0.3),
        ])

        config = RouteWiseConfig(concurrency_enabled=True)
        router = RouteWiseRouter(fixed_router=fr, config=config)

        # S_A failed -> only other S_A in fallback.
        fallbacks = router._get_fallback_adapters("test-model", api_a)
        assert quota not in fallbacks
        assert conc not in fallbacks
        assert api_b in fallbacks

        # S_Q failed -> only S_A adapters in fallback.
        fallbacks = router._get_fallback_adapters("test-model", quota)
        assert quota not in fallbacks
        assert conc not in fallbacks
        assert api_a in fallbacks
        assert api_b in fallbacks


# ---------------------------------------------------------------------------
# PD / LA-PD decision logic tests (PR-3)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRouteWisePDDecision:

    def test_pd_routes_to_quota_when_value_exceeds_threshold(self):
        """When v_t >= theta_Q and quota remains, PD selects S_Q adapter."""
        config = RouteWiseConfig(
            decision_rule="pd",
            daily_quota=10000,
            shadow_price_L_seed=0.0000001,
            shadow_price_U_seed=0.001,
        )
        router, quota_adapter, api_adapter = _make_router_with_quota_and_api(
            config=config, prompt_price="3.0", completion_price="15.0"
        )
        # Warm the predictor so v_t is meaningful.
        for _ in range(25):
            router.predictor.update("test-model", 500)

        initial_remaining = router.quota_mgr.remaining
        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is quota_adapter
        # Dispatch-commit: quota consumed at selection time.
        assert router.quota_mgr.remaining < initial_remaining

    def test_pd_routes_to_api_when_value_below_threshold(self):
        """When v_t < theta_Q, PD selects the cheapest S_A adapter."""
        # Very high shadow price bounds so theta_Q >> v_t.
        config = RouteWiseConfig(
            decision_rule="pd",
            daily_quota=10000,
            shadow_price_L_seed=1000.0,
            shadow_price_U_seed=10000.0,
        )
        router, quota_adapter, api_adapter = _make_router_with_quota_and_api(
            config=config, prompt_price="3.0", completion_price="15.0"
        )
        for _ in range(25):
            router.predictor.update("test-model", 500)

        initial_remaining = router.quota_mgr.remaining
        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is api_adapter
        # No dispatch-commit: quota unchanged.
        assert router.quota_mgr.remaining == initial_remaining

    def test_lapd_uses_conservative_lcb(self):
        """LA-PD uses q10 (LCB) instead of q50 for value estimation."""
        config = RouteWiseConfig(decision_rule="lapd", daily_quota=10000)
        router, quota_adapter, api_adapter = _make_router_with_quota_and_api(config=config)

        # Warm up predictor.
        for _ in range(25):
            router.predictor.update("test-model", 500)

        # v_t with lapd should use q10 (lower), giving smaller value.
        v_lapd = router._estimate_value("test-model", 1000)

        # Compare against pd.
        router.config.decision_rule = "pd"
        v_pd = router._estimate_value("test-model", 1000)

        # LA-PD value should be <= PD value because q10 <= q50.
        assert v_lapd <= v_pd

    def test_no_quota_adapter_always_selects_api(self):
        """Models with only S_A adapters never route to S_Q."""
        api_only = _make_adapter(subscription_type="api")
        fr = _FakeFixedRouter()
        fr.add("test-model", [(api_only, 1.0)])

        config = RouteWiseConfig(daily_quota=10000)
        router = RouteWiseRouter(fixed_router=fr, config=config)

        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is api_only

    def test_quota_exhausted_routes_to_api(self):
        """When quota is exhausted, PD routes to S_A even if value is high."""
        config = RouteWiseConfig(
            daily_quota=100,
            shadow_price_L_seed=0.0000001,
            shadow_price_U_seed=0.001,
        )
        router, quota_adapter, api_adapter = _make_router_with_quota_and_api(config=config)
        for _ in range(25):
            router.predictor.update("test-model", 500)

        # Exhaust the quota (100 requests).
        for _ in range(100):
            router.quota_mgr.consume()
        assert router.quota_mgr.remaining == 0

        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is api_adapter

    def test_context_prompt_tokens_used(self):
        """Explicit prompt_tokens from context flows into value estimation."""
        config = RouteWiseConfig(daily_quota=10000)
        router, _, _ = _make_router_with_quota_and_api(config=config)

        for _ in range(25):
            router.predictor.update("test-model", 500)

        v_small = router._estimate_value("test-model", 100)
        v_large = router._estimate_value("test-model", 10000)
        assert v_large > v_small

    def test_prompt_tokens_estimated_from_messages(self):
        """When prompt_tokens is absent, tokens are estimated from messages."""
        config = RouteWiseConfig(
            daily_quota=10000,
            shadow_price_L_seed=0.0000001,
            shadow_price_U_seed=0.001,
        )
        router, quota_adapter, api_adapter = _make_router_with_quota_and_api(config=config)
        for _ in range(25):
            router.predictor.update("test-model", 500)

        # Build messages with ~4000 chars (~1000 tokens).
        long_msg = "x" * 4000
        context_with_messages = {
            "messages": [{"role": "user", "content": long_msg}],
        }
        # Should still route correctly (not silently use 0 prompt_tokens).
        initial_remaining = router.quota_mgr.remaining
        selected = router._select_adapter("test-model", context_with_messages)
        assert selected is quota_adapter
        # Quota consumed: one request slot.
        consumed = initial_remaining - router.quota_mgr.remaining
        assert consumed == 1

    def test_dispatch_commit_consumes_quota_at_selection(self):
        """Quota is consumed at dispatch time, not deferred to observation."""
        config = RouteWiseConfig(
            daily_quota=10000,
            shadow_price_L_seed=0.0000001,
            shadow_price_U_seed=0.001,
        )
        router, quota_adapter, api_adapter = _make_router_with_quota_and_api(config=config)
        for _ in range(25):
            router.predictor.update("test-model", 500)

        before = router.quota_mgr.remaining
        selected = router._select_adapter("test-model", {"prompt_tokens": 200})
        assert selected is quota_adapter

        after = router.quota_mgr.remaining
        # Exactly one request slot consumed per dispatch-commit.
        assert before - after == 1


@pytest.mark.unit
class TestRouteWiseObservation:

    def test_record_observation_updates_predictor(self):
        """Predictor state changes after recording an observation."""
        router, _, _ = _make_router_with_quota_and_api()

        obs = RoutingObservation(
            model_id="test-model",
            endpoint_id="test-model:api-provider",
            ttft_ms=50.0,
            total_latency_ms=200.0,
            token_count=600,
            prompt_tokens=100,
            completion_tokens=500,
            success=True,
            quota_committed=0.0,
        )
        router.record_observation(obs)

        # Predictor should have recorded the completion tokens.
        state = router.predictor._model_states.get("test-model")
        assert state is not None
        assert state.count == 1
        assert state.mean == 500.0

    def test_record_observation_does_not_consume_quota(self):
        """Quota is consumed at dispatch time, not in record_observation.

        Even an observation reporting a quota-routed endpoint must not
        double-count quota.
        """
        config = RouteWiseConfig(daily_quota=5000)
        router, quota_adapter, api_adapter = _make_router_with_quota_and_api(config=config)

        initial_remaining = router.quota_mgr.remaining

        # Simulate observation from a quota adapter.
        obs = RoutingObservation(
            model_id="test-model",
            endpoint_id="test-model:quota-provider",
            ttft_ms=50.0,
            total_latency_ms=200.0,
            token_count=600,
            prompt_tokens=100,
            completion_tokens=500,
            success=True,
            quota_committed=0.0,
        )
        router.record_observation(obs)

        # Quota must NOT change in record_observation.
        assert router.quota_mgr.remaining == initial_remaining

    def test_record_observation_api_does_not_consume_quota(self):
        """Quota is untouched for S_A routed observations."""
        config = RouteWiseConfig(daily_quota=5000)
        router, quota_adapter, api_adapter = _make_router_with_quota_and_api(config=config)

        initial_remaining = router.quota_mgr.remaining

        obs = RoutingObservation(
            model_id="test-model",
            endpoint_id="test-model:api-provider",
            ttft_ms=50.0,
            total_latency_ms=200.0,
            token_count=600,
            prompt_tokens=100,
            completion_tokens=500,
            success=True,
            quota_committed=0.0,
        )
        router.record_observation(obs)

        assert router.quota_mgr.remaining == initial_remaining

    def test_record_observation_does_not_raise(self):
        """record_observation never raises, even with edge-case data."""
        fr = _FakeFixedRouter()
        router = RouteWiseRouter(fixed_router=fr, config=RouteWiseConfig())
        obs = RoutingObservation(
            model_id="unknown-model",
            endpoint_id="unknown:provider",
            ttft_ms=None,
            total_latency_ms=0.0,
            token_count=0,
            prompt_tokens=0,
            completion_tokens=0,
            success=False,
            quota_committed=0.0,
        )
        router.record_observation(obs)  # Should not raise.
