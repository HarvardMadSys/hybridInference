"""Tests for RouteWise router -- scaffold + PD / LA-PD decision logic + Layer 2 + S_C."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routing.routers import RoutingObservation
from routing.routewise.config import RouteWiseConfig
from routing.routewise.hedging import HedgedAdapter
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
    # Concrete (JSON-serializable) base_url so the synthetic _routing chunk
    # emitted by FixedRouter.stream_chat_completion can be json.dumps()'d.
    cfg.base_url = f"https://{provider}.example/v1"
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

    def add(self, model_id: str, adapters_with_weights: list[tuple[Any, float]]) -> None:
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

        router = RouteWiseRouter(fixed_router=fr, config=RouteWiseConfig())
        selected = router._select_adapter("test-model", {})
        assert selected is adapter

    def test_unregistered_model_raises(self):
        """Requesting an unknown model raises ValueError."""
        fr = _FakeFixedRouter()
        router = RouteWiseRouter(fixed_router=fr, config=RouteWiseConfig())
        with pytest.raises(ValueError, match="no route"):
            router._select_adapter("nonexistent", {})

    def test_subscription_type_classification(self):
        """Adapters are correctly classified by their subscription_type."""
        quota_adapter = _make_adapter(subscription_type="quota")
        api_adapter = _make_adapter(subscription_type="api")

        fr = _FakeFixedRouter()
        fr.add("test-model", [(quota_adapter, 0.5), (api_adapter, 0.5)])

        router = RouteWiseRouter(fixed_router=fr, config=RouteWiseConfig())
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
            subscription_type="quota",
            prompt_price="3.0",
            completion_price="15.0",
            endpoint_id="test-model:quota-provider",
        )
        api_adapter = _make_adapter(
            subscription_type="api",
            prompt_price="3.0",
            completion_price="15.0",
            endpoint_id="test-model:api-provider",
        )

        fr = _FakeFixedRouter()
        fr.add("test-model", [(api_adapter, 0.5), (quota_adapter, 0.5)])

        router = RouteWiseRouter(fixed_router=fr, config=RouteWiseConfig())
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

        router = RouteWiseRouter(fixed_router=fr, config=RouteWiseConfig())
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
            subscription_type="api",
            prompt_price="0.5",
            completion_price="20.0",
        )
        provider_b = _make_adapter(
            subscription_type="api",
            prompt_price="5.0",
            completion_price="5.0",
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

        router = RouteWiseRouter(fixed_router=fr, config=RouteWiseConfig())
        fallbacks = router._get_fallback_adapters("test-model", a1)
        assert a1 not in fallbacks
        assert a2 in fallbacks

    def test_fallback_empty_for_unknown_model(self):
        """Fallback returns empty list for an unregistered model."""
        fr = _FakeFixedRouter()
        router = RouteWiseRouter(fixed_router=fr, config=RouteWiseConfig())
        assert router._get_fallback_adapters("nonexistent", MagicMock()) == []

    def test_unknown_subscription_type_defaults_to_api(self):
        """Unknown subscription_type value falls back to API."""
        adapter = _make_adapter(subscription_type="unknown_tier")
        fr = _FakeFixedRouter()
        fr.add("test-model", [(adapter, 1.0)])

        router = RouteWiseRouter(fixed_router=fr, config=RouteWiseConfig())
        entries = router.classified["test-model"]
        assert entries[0][2] is SubscriptionType.API

    def test_attach_fixed_router_clears_derived_state_on_rebind(self):
        """Rebinding resets all derived state that depends on prior routing activity."""
        adapter = _make_adapter()
        fr = _FakeFixedRouter()
        fr.add("test-model", [(adapter, 1.0)])

        router = RouteWiseRouter(fixed_router=fr, config=RouteWiseConfig())
        router._pending_decisions["req-1"] = {"decision": "quota"}
        router._shadow_hedge_log.append(MagicMock())
        router._pending_lp_solves.add("test-model")

        replacement = _FakeFixedRouter()
        replacement.add("test-model", [(adapter, 1.0)])

        router.attach_fixed_router(replacement)

        assert router.fixed_router is replacement
        assert router._pending_decisions == {}
        assert router._shadow_hedge_log == []
        assert router._pending_lp_solves == set()

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
        fr.add(
            "test-model",
            [
                (quota, 0.2),
                (conc, 0.2),
                (api_a, 0.3),
                (api_b, 0.3),
            ],
        )

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
        router, quota_adapter, _api_adapter = _make_router_with_quota_and_api(
            config=config, prompt_price="3.0", completion_price="15.0"
        )
        # Warm the predictor so v_t is meaningful.
        for _ in range(25):
            router.predictor.update("test-model", 500)

        initial_remaining = router.quota_mgr.remaining
        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is quota_adapter
        # Selection-commit: quota consumed at selection time.
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
        router, _quota_adapter, api_adapter = _make_router_with_quota_and_api(
            config=config, prompt_price="3.0", completion_price="15.0"
        )
        for _ in range(25):
            router.predictor.update("test-model", 500)

        initial_remaining = router.quota_mgr.remaining
        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is api_adapter
        # No selection-commit: quota unchanged.
        assert router.quota_mgr.remaining == initial_remaining

    def test_lapd_uses_conservative_lcb(self):
        """LA-PD uses q10 (LCB) instead of q50 for value estimation."""
        config = RouteWiseConfig(decision_rule="lapd", daily_quota=10000)
        router, _quota_adapter, _api_adapter = _make_router_with_quota_and_api(config=config)

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
        router, _quota_adapter, api_adapter = _make_router_with_quota_and_api(config=config)
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
        router, quota_adapter, _api_adapter = _make_router_with_quota_and_api(config=config)
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

    def test_selection_commit_consumes_quota_at_selection(self):
        """Quota is consumed at selection time, not deferred to observation."""
        config = RouteWiseConfig(
            daily_quota=10000,
            shadow_price_L_seed=0.0000001,
            shadow_price_U_seed=0.001,
        )
        router, quota_adapter, _api_adapter = _make_router_with_quota_and_api(config=config)
        for _ in range(25):
            router.predictor.update("test-model", 500)

        before = router.quota_mgr.remaining
        selected = router._select_adapter("test-model", {"prompt_tokens": 200})
        assert selected is quota_adapter

        after = router.quota_mgr.remaining
        # Exactly one request slot consumed per selection-commit.
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
        router, _quota_adapter, _api_adapter = _make_router_with_quota_and_api(config=config)

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
        router, _quota_adapter, _api_adapter = _make_router_with_quota_and_api(config=config)

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


# ---------------------------------------------------------------------------
# Layer 2: Latency-aware provider selection tests (PR-4)
# ---------------------------------------------------------------------------


def _make_router_with_two_api(
    config: RouteWiseConfig | None = None,
) -> tuple[RouteWiseRouter, MagicMock, MagicMock]:
    """Build a RouteWiseRouter with two S_A adapters (no S_Q).

    Returns:
        (router, api_adapter_a, api_adapter_b)
    """
    if config is None:
        config = RouteWiseConfig()
    api_a = _make_adapter(
        subscription_type="api",
        prompt_price="3.0",
        completion_price="15.0",
        endpoint_id="test-model:api-a",
    )
    api_b = _make_adapter(
        subscription_type="api",
        prompt_price="4.0",
        completion_price="20.0",
        endpoint_id="test-model:api-b",
    )
    fr = _FakeFixedRouter()
    fr.add("test-model", [(api_a, 0.5), (api_b, 0.5)])
    router = RouteWiseRouter(fixed_router=fr, config=config)
    return router, api_a, api_b


@pytest.mark.unit
class TestRouteWiseLayer2:
    def test_layer2_uses_lp_when_warmed(self):
        """After profile warmup, LP path is used for S_A selection."""
        config = RouteWiseConfig(
            latency_min_samples=5,
            latency_lp_interval_sec=0.0,  # Always re-solve.
            latency_slo_sec=2.0,
        )
        router, _api_a, _api_b = _make_router_with_two_api(config)

        # Warm predictor.
        for _ in range(25):
            router.predictor.update("test-model", 500)

        # Populate latency profiles for both endpoints.
        now = time.time()
        for _ in range(10):
            router._latency_profiles["test-model:api-a"].record(now, 200.0)
            router._latency_profiles["test-model:api-b"].record(now, 300.0)

        # Select adapter -- should use LP path.
        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is not None

        # LP should have run: check that last_lp_status was updated.
        assert router._last_lp_statuses.get("test-model") != "not_run"

    def test_layer2_falls_back_to_cheapest_when_cold(self):
        """When profiles have insufficient samples, falls back to cheapest."""
        config = RouteWiseConfig(
            latency_min_samples=100,  # Very high threshold.
        )
        router, api_a, _api_b = _make_router_with_two_api(config)

        # Warm predictor.
        for _ in range(25):
            router.predictor.update("test-model", 500)

        # No latency data -> cold start -> cheapest API.
        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        # api_a is cheaper (3.0/15.0 vs 4.0/20.0).
        assert selected is api_a

    def test_record_observation_updates_latency_profile(self):
        """Observation flows to the endpoint's ProviderProfile."""
        config = RouteWiseConfig()
        router, _api_a, _api_b = _make_router_with_two_api(config)

        obs = RoutingObservation(
            model_id="test-model",
            endpoint_id="test-model:api-a",
            ttft_ms=150.0,
            total_latency_ms=500.0,
            token_count=600,
            prompt_tokens=100,
            completion_tokens=500,
            success=True,
            quota_committed=0.0,
        )
        router.record_observation(obs)

        # Profile should have 1 sample.
        profile = router._latency_profiles["test-model:api-a"]
        now = time.time()
        assert profile.sample_count(now) == 1

    def test_single_api_skips_lp(self):
        """Single S_A provider bypasses Layer 2 LP."""
        api_only = _make_adapter(
            subscription_type="api",
            prompt_price="3.0",
            completion_price="15.0",
            endpoint_id="test-model:api-only",
        )
        fr = _FakeFixedRouter()
        fr.add("test-model", [(api_only, 1.0)])

        config = RouteWiseConfig(latency_min_samples=1)
        router = RouteWiseRouter(fixed_router=fr, config=config)

        # Warm predictor.
        for _ in range(25):
            router.predictor.update("test-model", 500)

        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is api_only
        # LP should not have run.
        assert router._last_lp_statuses.get("test-model", "not_run") == "not_run"

    def test_shadow_hedge_logged(self):
        """Shadow mode produces log entries when multiple providers exist."""
        config = RouteWiseConfig(
            latency_min_samples=5,
            latency_lp_interval_sec=0.0,
            latency_slo_sec=2.0,
            latency_hedge_mode="shadow",
        )
        router, _api_a, _api_b = _make_router_with_two_api(config)

        # Warm predictor.
        for _ in range(25):
            router.predictor.update("test-model", 500)

        # Populate profiles.
        now = time.time()
        for _ in range(10):
            router._latency_profiles["test-model:api-a"].record(now, 200.0)
            router._latency_profiles["test-model:api-b"].record(now, 300.0)

        router._select_adapter("test-model", {"prompt_tokens": 1000})

        # Shadow hedge log should have at least one entry with correct model_id.
        assert len(router._shadow_hedge_log) >= 1
        entry = router._shadow_hedge_log[0]
        assert entry.model_id == "test-model"
        assert entry.reason in (
            "no_backup",
            "hedge_not_justified",
            "hedge_warranted",
            "insufficient_samples",
        )

    def test_error_observation_updates_profile(self):
        """Failed observation records error in the profile."""
        config = RouteWiseConfig()
        router, _api_a, _api_b = _make_router_with_two_api(config)

        obs = RoutingObservation(
            model_id="test-model",
            endpoint_id="test-model:api-a",
            ttft_ms=None,
            total_latency_ms=5000.0,
            token_count=0,
            prompt_tokens=100,
            completion_tokens=0,
            success=False,
            quota_committed=0.0,
        )
        router.record_observation(obs)

        profile = router._latency_profiles["test-model:api-a"]
        now = time.time()
        assert profile.error_rate(now) > 0

    def test_multi_model_layer2_isolation(self):
        """Two models sharing one RouteWiseRouter have independent LP state.

        Model A's LP solve must NOT pollute model B's SWRR sampler, and
        model B's LP interval check must be independent of model A.
        """
        config = RouteWiseConfig(
            latency_min_samples=5,
            latency_lp_interval_sec=0.0,  # Always re-solve.
            latency_slo_sec=2.0,
        )

        # Two models, each with two S_A endpoints.
        a1 = _make_adapter(
            subscription_type="api",
            prompt_price="3.0",
            completion_price="15.0",
            endpoint_id="model-a:ep1",
        )
        a2 = _make_adapter(
            subscription_type="api",
            prompt_price="4.0",
            completion_price="20.0",
            endpoint_id="model-a:ep2",
        )
        b1 = _make_adapter(
            subscription_type="api",
            prompt_price="5.0",
            completion_price="10.0",
            endpoint_id="model-b:ep1",
        )
        b2 = _make_adapter(
            subscription_type="api",
            prompt_price="6.0",
            completion_price="12.0",
            endpoint_id="model-b:ep2",
        )

        fr = _FakeFixedRouter()
        fr.add("model-a", [(a1, 0.5), (a2, 0.5)])
        fr.add("model-b", [(b1, 0.5), (b2, 0.5)])

        router = RouteWiseRouter(fixed_router=fr, config=config)

        # Warm predictors for both models.
        for _ in range(25):
            router.predictor.update("model-a", 500)
            router.predictor.update("model-b", 500)

        # Populate latency profiles for all endpoints.
        now = time.time()
        for _ in range(10):
            router._latency_profiles["model-a:ep1"].record(now, 200.0)
            router._latency_profiles["model-a:ep2"].record(now, 300.0)
            router._latency_profiles["model-b:ep1"].record(now, 400.0)
            router._latency_profiles["model-b:ep2"].record(now, 500.0)

        # Route model A.
        sel_a = router._select_adapter("model-a", {"prompt_tokens": 1000})
        assert sel_a is not None
        assert sel_a in (a1, a2), "Model A must select from its own endpoints"

        # Route model B.
        sel_b = router._select_adapter("model-b", {"prompt_tokens": 1000})
        assert sel_b is not None
        assert sel_b in (b1, b2), "Model B must select from its own endpoints"

        # LP statuses are independent per model.
        assert router._last_lp_statuses.get("model-a") is not None
        assert router._last_lp_statuses.get("model-b") is not None

        # SWRR samplers are independent per model.
        a_weights = router._swrr_samplers["model-a"].get_weights()
        b_weights = router._swrr_samplers["model-b"].get_weights()
        # A's weights must only contain A's endpoints.
        for eid in a_weights:
            assert eid.startswith("model-a:")
        # B's weights must only contain B's endpoints.
        for eid in b_weights:
            assert eid.startswith("model-b:")


# ---------------------------------------------------------------------------
# S_C Concurrency tier tests (PR-6)
# ---------------------------------------------------------------------------


def _make_router_with_conc_and_api(
    config: RouteWiseConfig | None = None,
    prompt_price: str = "3.0",
    completion_price: str = "15.0",
) -> tuple[RouteWiseRouter, MagicMock, MagicMock]:
    """Build a RouteWiseRouter with one S_C and one S_A adapter.

    Returns:
        (router, conc_adapter, api_adapter)
    """
    if config is None:
        config = RouteWiseConfig(concurrency_enabled=True, concurrency_limit=4)
    conc_adapter = _make_adapter(
        subscription_type="concurrency",
        prompt_price=prompt_price,
        completion_price=completion_price,
        endpoint_id="test-model:conc-provider",
    )
    api_adapter = _make_adapter(
        subscription_type="api",
        prompt_price=prompt_price,
        completion_price=completion_price,
        endpoint_id="test-model:api-provider",
    )
    fr = _FakeFixedRouter()
    fr.add("test-model", [(conc_adapter, 0.5), (api_adapter, 0.5)])
    router = RouteWiseRouter(fixed_router=fr, config=config)
    return router, conc_adapter, api_adapter


def _make_router_three_tier(
    config: RouteWiseConfig | None = None,
) -> tuple[RouteWiseRouter, MagicMock, MagicMock, MagicMock]:
    """Build a RouteWiseRouter with S_C, S_Q, and S_A adapters.

    Returns:
        (router, conc_adapter, quota_adapter, api_adapter)
    """
    if config is None:
        config = RouteWiseConfig(
            concurrency_enabled=True,
            concurrency_limit=4,
            daily_quota=5000,
            shadow_price_L_seed=0.001,
            shadow_price_U_seed=0.500,
        )
    conc_adapter = _make_adapter(
        subscription_type="concurrency",
        prompt_price="3.0",
        completion_price="15.0",
        endpoint_id="test-model:conc-provider",
    )
    quota_adapter = _make_adapter(
        subscription_type="quota",
        prompt_price="3.0",
        completion_price="15.0",
        endpoint_id="test-model:quota-provider",
    )
    api_adapter = _make_adapter(
        subscription_type="api",
        prompt_price="3.0",
        completion_price="15.0",
        endpoint_id="test-model:api-provider",
    )
    fr = _FakeFixedRouter()
    fr.add(
        "test-model",
        [
            (conc_adapter, 0.3),
            (quota_adapter, 0.3),
            (api_adapter, 0.4),
        ],
    )
    router = RouteWiseRouter(fixed_router=fr, config=config)
    return router, conc_adapter, quota_adapter, api_adapter


@pytest.mark.unit
class TestRouteWiseSCDecision:
    """Layer 1 decision tests for S_C concurrency tier."""

    def test_sc_routes_to_concurrency_when_available(self):
        """S_C selected when slots are available."""
        router, conc_adapter, _api_adapter = _make_router_with_conc_and_api()

        # Warm predictor so v_t is meaningful.
        for _ in range(25):
            router.predictor.update("test-model", 500)

        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is conc_adapter
        assert router.conc_mgr.active == 1

    def test_sc_routes_to_api_when_full(self):
        """Falls to S_A when S_C slots are exhausted."""
        config = RouteWiseConfig(
            concurrency_enabled=True,
            concurrency_limit=1,
        )
        router, _conc_adapter, api_adapter = _make_router_with_conc_and_api(config=config)

        for _ in range(25):
            router.predictor.update("test-model", 500)

        # Fill the single slot.
        router.conc_mgr.try_acquire()

        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is api_adapter

    def test_sc_preferred_over_sq_when_both_available(self):
        """S_C beats S_Q because gain_C = v_t > v_t - theta_Q = gain_Q."""
        config = RouteWiseConfig(
            concurrency_enabled=True,
            concurrency_limit=4,
            daily_quota=5000,
            shadow_price_L_seed=0.001,
            shadow_price_U_seed=0.500,
        )
        router, conc_adapter, _quota_adapter, _api_adapter = _make_router_three_tier(config=config)

        for _ in range(25):
            router.predictor.update("test-model", 500)

        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is conc_adapter

    def test_sc_skipped_when_disabled(self):
        """S_C adapter not selected when concurrency_enabled=False."""
        config = RouteWiseConfig(
            concurrency_enabled=False,
            concurrency_limit=4,
        )
        router, _conc_adapter, api_adapter = _make_router_with_conc_and_api(config=config)
        assert router.conc_mgr is None

        for _ in range(25):
            router.predictor.update("test-model", 500)

        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is api_adapter

    def test_sc_full_sq_available_routes_to_sq(self):
        """When S_C is full, falls to S_Q if theta_Q condition met."""
        config = RouteWiseConfig(
            concurrency_enabled=True,
            concurrency_limit=1,
            daily_quota=5000,
            shadow_price_L_seed=0.0000001,
            shadow_price_U_seed=0.001,
        )
        router, _conc_adapter, quota_adapter, _api_adapter = _make_router_three_tier(config=config)

        for _ in range(25):
            router.predictor.update("test-model", 500)

        # Fill S_C.
        router.conc_mgr.try_acquire()

        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is quota_adapter

    def test_sc_full_sq_exhausted_routes_to_api(self):
        """When both S_C and S_Q exhausted, routes to S_A."""
        config = RouteWiseConfig(
            concurrency_enabled=True,
            concurrency_limit=1,
            daily_quota=100,
            shadow_price_L_seed=0.0000001,
            shadow_price_U_seed=0.001,
        )
        router, _conc_adapter, _quota_adapter, api_adapter = _make_router_three_tier(config=config)

        for _ in range(25):
            router.predictor.update("test-model", 500)

        # Fill S_C.
        router.conc_mgr.try_acquire()
        # Exhaust S_Q.
        for _ in range(100):
            router.quota_mgr.consume()

        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is api_adapter

    def test_three_tier_priority_cascade(self):
        """Full cascade: S_C -> S_Q -> S_A as resources deplete."""
        config = RouteWiseConfig(
            concurrency_enabled=True,
            concurrency_limit=1,
            daily_quota=1,
            shadow_price_L_seed=0.0000001,
            shadow_price_U_seed=0.001,
        )
        router, conc_adapter, quota_adapter, api_adapter = _make_router_three_tier(config=config)

        for _ in range(25):
            router.predictor.update("test-model", 500)

        # First request: S_C (slot available).
        sel1 = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert sel1 is conc_adapter

        # S_C now full (limit=1). Second request: S_Q.
        sel2 = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert sel2 is quota_adapter

        # S_Q now exhausted (quota=1). Third request: S_A.
        sel3 = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert sel3 is api_adapter


@pytest.mark.unit
class TestRouteWiseSCLifecycle:
    """Async slot lifecycle tests for S_C concurrency tier."""

    @pytest.mark.asyncio
    async def test_slot_acquired_and_released_on_success(self):
        """Slot released after successful execution."""
        router, conc_adapter, _api_adapter = _make_router_with_conc_and_api()

        for _ in range(25):
            router.predictor.update("test-model", 500)

        # Select -> acquires slot.
        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is conc_adapter
        assert router.conc_mgr.active == 1

        # Mock super()._execute_adapter.
        with patch.object(
            type(router).__mro__[1],  # BaseRouter
            "_execute_adapter",
            new_callable=AsyncMock,
            return_value={"choices": [{"message": {"content": "ok"}}]},
        ):
            await router._execute_adapter(
                conc_adapter,
                "test-model",
                [{"role": "user", "content": "hi"}],
            )
        assert router.conc_mgr.active == 0

    @pytest.mark.asyncio
    async def test_slot_released_on_provider_error(self):
        """Slot released even when adapter raises an exception."""
        router, conc_adapter, _api_adapter = _make_router_with_conc_and_api()

        for _ in range(25):
            router.predictor.update("test-model", 500)

        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is conc_adapter
        assert router.conc_mgr.active == 1

        with (
            patch.object(
                type(router).__mro__[1],
                "_execute_adapter",
                new_callable=AsyncMock,
                side_effect=RuntimeError("provider error"),
            ),
            pytest.raises(RuntimeError, match="provider error"),
        ):
            await router._execute_adapter(
                conc_adapter,
                "test-model",
                [{"role": "user", "content": "hi"}],
            )
        assert router.conc_mgr.active == 0

    @pytest.mark.asyncio
    async def test_slot_released_on_cancel(self):
        """Slot released on asyncio.CancelledError."""
        router, conc_adapter, _api_adapter = _make_router_with_conc_and_api()

        for _ in range(25):
            router.predictor.update("test-model", 500)

        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is conc_adapter
        assert router.conc_mgr.active == 1

        with (
            patch.object(
                type(router).__mro__[1],
                "_execute_adapter",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError(),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await router._execute_adapter(
                conc_adapter,
                "test-model",
                [{"role": "user", "content": "hi"}],
            )
        assert router.conc_mgr.active == 0

    @pytest.mark.asyncio
    async def test_stream_slot_released_on_completion(self):
        """Streaming: slot released after generator exhaustion."""
        router, conc_adapter, _api_adapter = _make_router_with_conc_and_api()

        for _ in range(25):
            router.predictor.update("test-model", 500)

        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is conc_adapter
        assert router.conc_mgr.active == 1

        async def _fake_stream(*args, **kwargs):
            yield {"choices": [{"delta": {"content": "hello"}}]}
            yield {"choices": [{"delta": {"content": " world"}}]}

        with patch.object(
            type(router).__mro__[1],
            "_execute_stream_adapter",
            side_effect=_fake_stream,
        ):
            chunks = []
            async for chunk in router._execute_stream_adapter(
                conc_adapter,
                "test-model",
                [{"role": "user", "content": "hi"}],
            ):
                chunks.append(chunk)
            assert len(chunks) == 2
        assert router.conc_mgr.active == 0

    @pytest.mark.asyncio
    async def test_no_slot_for_api_request(self):
        """S_A execution does not touch conc_mgr."""
        config = RouteWiseConfig(
            concurrency_enabled=True,
            concurrency_limit=1,
        )
        router, _conc_adapter, api_adapter = _make_router_with_conc_and_api(config=config)

        for _ in range(25):
            router.predictor.update("test-model", 500)

        # Fill S_C so next request goes to S_A.
        router.conc_mgr.try_acquire()
        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is api_adapter
        assert router.conc_mgr.active == 1  # From manual acquire.

        with patch.object(
            type(router).__mro__[1],
            "_execute_adapter",
            new_callable=AsyncMock,
            return_value={"choices": [{"message": {"content": "ok"}}]},
        ):
            await router._execute_adapter(
                api_adapter,
                "test-model",
                [{"role": "user", "content": "hi"}],
            )
        # conc_mgr unchanged -- S_A doesn't release.
        assert router.conc_mgr.active == 1

    @pytest.mark.asyncio
    async def test_no_slot_for_quota_request(self):
        """S_Q execution does not touch conc_mgr."""
        config = RouteWiseConfig(
            concurrency_enabled=True,
            concurrency_limit=4,
            daily_quota=5000,
            shadow_price_L_seed=0.0000001,
            shadow_price_U_seed=0.001,
        )
        # Need a router with S_Q + S_C + S_A.
        router, _conc_adapter, quota_adapter, _api_adapter = _make_router_three_tier(config=config)

        for _ in range(25):
            router.predictor.update("test-model", 500)

        # Fill S_C so next request falls to S_Q.
        for _ in range(4):
            router.conc_mgr.try_acquire()
        assert router.conc_mgr.active == 4

        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})
        assert selected is quota_adapter

        with patch.object(
            type(router).__mro__[1],
            "_execute_adapter",
            new_callable=AsyncMock,
            return_value={"choices": [{"message": {"content": "ok"}}]},
        ):
            await router._execute_adapter(
                quota_adapter,
                "test-model",
                [{"role": "user", "content": "hi"}],
            )
        # conc_mgr unchanged -- S_Q doesn't release.
        assert router.conc_mgr.active == 4


# ---------------------------------------------------------------------------
# No-S_A edge case tests (codex review fix)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRouteWiseNoApiBaseline:
    """Ensure no accounting bypass when S_A baseline is absent."""

    def test_sc_only_full_returns_none(self):
        """S_C-only config: when slots are full, returns None (not S_C)."""
        conc = _make_adapter(
            subscription_type="concurrency",
            endpoint_id="test-model:conc",
        )
        fr = _FakeFixedRouter()
        fr.add("test-model", [(conc, 1.0)])

        config = RouteWiseConfig(concurrency_enabled=True, concurrency_limit=1)
        router = RouteWiseRouter(fixed_router=fr, config=config)

        # Fill the single slot externally.
        router.conc_mgr.try_acquire()
        assert router.conc_mgr.active == 1

        selected = router._select_adapter("test-model", {"prompt_tokens": 100})
        assert selected is None
        # Active must not change -- no spurious acquire or release.
        assert router.conc_mgr.active == 1

    def test_sq_only_exhausted_returns_none(self):
        """S_Q-only config: when quota exhausted, returns None (not S_Q)."""
        quota = _make_adapter(
            subscription_type="quota",
            endpoint_id="test-model:quota",
        )
        fr = _FakeFixedRouter()
        fr.add("test-model", [(quota, 1.0)])

        config = RouteWiseConfig(daily_quota=5)
        router = RouteWiseRouter(fixed_router=fr, config=config)

        # Exhaust quota.
        for _ in range(5):
            router.quota_mgr.consume()
        assert router.quota_mgr.remaining == 0

        selected = router._select_adapter("test-model", {"prompt_tokens": 100})
        assert selected is None
        # Quota must not change.
        assert router.quota_mgr.remaining == 0

    def test_sc_sq_no_api_all_depleted_returns_none(self):
        """S_C + S_Q but no S_A: returns None when both depleted."""
        conc = _make_adapter(
            subscription_type="concurrency",
            endpoint_id="test-model:conc",
        )
        quota = _make_adapter(
            subscription_type="quota",
            endpoint_id="test-model:quota",
        )
        fr = _FakeFixedRouter()
        fr.add("test-model", [(conc, 0.5), (quota, 0.5)])

        config = RouteWiseConfig(
            concurrency_enabled=True,
            concurrency_limit=1,
            daily_quota=3,
        )
        router = RouteWiseRouter(fixed_router=fr, config=config)

        # Fill S_C.
        router.conc_mgr.try_acquire()
        # Exhaust S_Q.
        for _ in range(3):
            router.quota_mgr.consume()

        selected = router._select_adapter("test-model", {"prompt_tokens": 100})
        assert selected is None
        assert router.conc_mgr.active == 1
        assert router.quota_mgr.remaining == 0

    def test_sc_only_available_still_routes(self):
        """S_C-only config: routes to S_C when slots available (v_t=inf ok)."""
        conc = _make_adapter(
            subscription_type="concurrency",
            endpoint_id="test-model:conc",
        )
        fr = _FakeFixedRouter()
        fr.add("test-model", [(conc, 1.0)])

        config = RouteWiseConfig(concurrency_enabled=True, concurrency_limit=4)
        router = RouteWiseRouter(fixed_router=fr, config=config)

        selected = router._select_adapter("test-model", {"prompt_tokens": 100})
        assert selected is conc
        assert router.conc_mgr.active == 1

    def test_validation_warns_no_api_baseline(self):
        """Construction-time warning when model has no S_A adapter."""
        conc = _make_adapter(
            subscription_type="concurrency",
            endpoint_id="test-model:conc",
        )
        fr = _FakeFixedRouter()
        fr.add("test-model", [(conc, 1.0)])

        config = RouteWiseConfig(concurrency_enabled=True, concurrency_limit=4)
        with patch("routing.routewise.router.logger") as mock_logger:
            RouteWiseRouter(fixed_router=fr, config=config)
            mock_logger.warning.assert_called()
            warning_msg = mock_logger.warning.call_args[0][0]
            assert "no S_A" in warning_msg


# ---------------------------------------------------------------------------
# Decision metadata tests (V2 observation plumbing)
# ---------------------------------------------------------------------------


def _make_router_with_all_tiers(
    config: RouteWiseConfig | None = None,
) -> tuple[RouteWiseRouter, MagicMock, MagicMock, MagicMock]:
    """Build a RouteWiseRouter with one S_C, one S_Q, and one S_A adapter.

    Returns:
        (router, conc_adapter, quota_adapter, api_adapter)
    """
    if config is None:
        config = RouteWiseConfig(concurrency_enabled=True, concurrency_limit=4)
    conc_adapter = _make_adapter(
        subscription_type="concurrency",
        prompt_price="3.0",
        completion_price="15.0",
        endpoint_id="test-model:conc-provider",
    )
    quota_adapter = _make_adapter(
        subscription_type="quota",
        prompt_price="3.0",
        completion_price="15.0",
        endpoint_id="test-model:quota-provider",
    )
    api_adapter = _make_adapter(
        subscription_type="api",
        prompt_price="3.0",
        completion_price="15.0",
        endpoint_id="test-model:api-provider",
    )
    fr = _FakeFixedRouter()
    fr.add(
        "test-model",
        [(conc_adapter, 0.3), (quota_adapter, 0.3), (api_adapter, 0.4)],
    )
    router = RouteWiseRouter(fixed_router=fr, config=config)
    return router, conc_adapter, quota_adapter, api_adapter


@pytest.mark.unit
class TestRouteWiseDecisionMetadata:
    """Verify _pending_decisions is populated and merged into responses."""

    def test_sc_decision_stores_metadata(self):
        """S_C selection stores metadata with selected_tier='concurrency'."""
        router, conc, _quota, _api = _make_router_with_all_tiers()
        request_id = "req-test-sc"
        context = {"request_id": request_id}

        selected = router._select_adapter("test-model", context)
        assert selected is conc
        assert request_id in router._pending_decisions

        meta = router._pending_decisions[request_id]
        assert meta["selected_tier"] == "concurrency"
        assert meta["sc_committed"] is True
        assert meta["quota_committed"] == 0.0
        assert meta["hedged"] is False
        assert meta["backup_won"] is False

    def test_sq_decision_stores_metadata(self):
        """S_Q selection stores metadata with selected_tier='quota'."""
        router, quota, _api = _make_router_with_quota_and_api()
        request_id = "req-test-sq"
        context = {"request_id": request_id}

        selected = router._select_adapter("test-model", context)
        assert selected is quota
        assert request_id in router._pending_decisions

        meta = router._pending_decisions[request_id]
        assert meta["selected_tier"] == "quota"
        assert meta["quota_committed"] == 0.0  # commitment signal via selected_tier, not v_t
        assert meta["v_t"] > 0  # value estimation lives in its own field
        assert meta["sc_committed"] is False
        assert meta["hedged"] is False

    def test_sa_decision_stores_metadata(self):
        """S_A selection stores metadata with selected_tier='api'."""
        # Make quota too expensive by setting high shadow price seed.
        config = RouteWiseConfig(daily_quota=1000, shadow_price_L_seed=1000.0)
        router, _quota, api = _make_router_with_quota_and_api(config=config)
        request_id = "req-test-sa"
        context = {"request_id": request_id}

        selected = router._select_adapter("test-model", context)
        assert selected is api
        assert request_id in router._pending_decisions

        meta = router._pending_decisions[request_id]
        assert meta["selected_tier"] == "api"
        assert meta["quota_committed"] == 0.0
        assert meta["sc_committed"] is False

    def test_no_request_id_still_works(self):
        """Selection works without request_id (no metadata stored)."""
        router, _quota, _api = _make_router_with_quota_and_api()
        # No request_id in context
        selected = router._select_adapter("test-model", {})
        assert selected is not None
        # _pending_decisions should remain empty
        assert len(router._pending_decisions) == 0

    @pytest.mark.asyncio
    async def test_chat_completion_merges_routewise_into_routing(self):
        """chat_completion() merges routewise metadata into resp['_routing']."""
        router, quota, api = _make_router_with_quota_and_api()

        # Mock the adapter to return a response
        quota.chat_completion = AsyncMock(
            return_value={"choices": [{"message": {"content": "hello"}}]}
        )
        api.chat_completion = AsyncMock(
            return_value={"choices": [{"message": {"content": "hello"}}]}
        )

        resp = await router.chat_completion(
            "test-model",
            [{"role": "user", "content": "hi"}],
            request_id="req-merge-test",
        )

        assert "_routing" in resp
        assert "routewise" in resp["_routing"]
        rw = resp["_routing"]["routewise"]
        assert rw["selected_tier"] in ("quota", "api")
        assert "v_t" in rw
        # _pending_decisions should be cleaned up
        assert "req-merge-test" not in router._pending_decisions

    @pytest.mark.asyncio
    async def test_stream_injects_routewise_chunk(self):
        """stream_chat_completion() injects a routewise chunk before [DONE]."""
        router, quota, api = _make_router_with_quota_and_api()

        async def _fake_stream(*args, **kwargs):
            yield 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            yield "data: [DONE]\n\n"

        quota.stream_chat_completion = _fake_stream
        api.stream_chat_completion = _fake_stream

        chunks = []
        async for chunk in router.stream_chat_completion(
            "test-model",
            [{"role": "user", "content": "hi"}],
            request_id="req-stream-test",
        ):
            chunks.append(chunk)

        # Should have: content chunk, routewise chunk, [DONE]
        assert len(chunks) >= 2

        # Find the routewise metadata chunk
        routewise_found = False
        for c in chunks:
            if isinstance(c, str) and c.startswith("data: ") and "routewise" in c:
                parsed = json.loads(c[6:])
                assert "_routing" in parsed
                assert "routewise" in parsed["_routing"]
                assert parsed["_routing"]["routewise"]["selected_tier"] in (
                    "quota",
                    "api",
                )
                routewise_found = True
                break
        assert routewise_found, "Routewise metadata chunk not found in stream"

        # [DONE] should be last
        assert chunks[-1].strip() == "data: [DONE]"

        # Cleanup
        assert "req-stream-test" not in router._pending_decisions

    @pytest.mark.asyncio
    async def test_pending_decisions_cleaned_on_error(self):
        """_pending_decisions is cleaned up when chat_completion raises."""
        router, quota, api = _make_router_with_quota_and_api()

        # Both adapters fail
        quota.chat_completion = AsyncMock(side_effect=RuntimeError("fail"))
        api.chat_completion = AsyncMock(side_effect=RuntimeError("fail"))

        with pytest.raises(RuntimeError):
            await router.chat_completion(
                "test-model",
                [{"role": "user", "content": "hi"}],
                request_id="req-error-test",
            )

        # Should be cleaned up
        assert "req-error-test" not in router._pending_decisions

    @pytest.mark.asyncio
    async def test_backup_won_detected_on_config_swap(self):
        """backup_won is set when HedgedAdapter swaps config."""
        router, _quota, _api = _make_router_with_quota_and_api()

        # Create a mock HedgedAdapter that swaps config on execution
        primary_adapter = _make_adapter(
            provider="primary",
            endpoint_id="test-model:primary",
        )
        backup_adapter = _make_adapter(
            provider="backup",
            endpoint_id="test-model:backup",
        )

        # Create a real HedgedAdapter
        hedged = HedgedAdapter(
            primary=primary_adapter,
            backup=backup_adapter,
            hedge_threshold_sec=0.0,
            event_sink=router,
        )

        # Pre-populate _pending_decisions as if _select_adapter ran
        request_id = "req-backup-test"
        router._pending_decisions[request_id] = {
            "selected_tier": "api",
            "hedged": True,
            "backup_won": False,
            "quota_committed": 0.0,
            "sc_committed": False,
            "lp_status": None,
            "v_t": 0.01,
            "gain_c": float("-inf"),
            "gain_q": float("-inf"),
            "gain_a": 0.0,
            "theta_q": None,
            "quota_remaining": 1000,
            "sc_active": 0,
            "sc_limit": 0,
        }

        # Simulate primary failing (slow), backup winning
        async def _slow_primary(messages, **params):
            await asyncio.sleep(10)
            return {"choices": [{"message": {"content": "primary"}}]}

        async def _fast_backup(messages, **params):
            return {"choices": [{"message": {"content": "backup"}}]}

        primary_adapter.chat_completion = _slow_primary
        backup_adapter.chat_completion = _fast_backup

        # Execute through RouteWise's _execute_adapter
        await router._execute_adapter(
            hedged,
            "test-model",
            [{"role": "user", "content": "hi"}],
            request_id=request_id,
        )

        # HedgedAdapter should have swapped config -> backup won
        assert hedged.config is backup_adapter.config
        assert router._pending_decisions[request_id]["backup_won"] is True

    @pytest.mark.asyncio
    async def test_exception_carries_routewise_in_routing(self):
        """When chat_completion fails, exception._routing contains routewise metadata."""
        router, quota, api = _make_router_with_quota_and_api()

        # Both adapters fail
        quota.chat_completion = AsyncMock(side_effect=RuntimeError("fail"))
        api.chat_completion = AsyncMock(side_effect=RuntimeError("fail"))

        with pytest.raises(RuntimeError) as exc_info:
            await router.chat_completion(
                "test-model",
                [{"role": "user", "content": "hi"}],
                request_id="req-exc-meta",
            )

        exc = exc_info.value
        routing = getattr(exc, "_routing", None)
        assert routing is not None
        assert "routewise" in routing
        rw = routing["routewise"]
        assert rw["selected_tier"] in ("quota", "api")
        assert "v_t" in rw

        # _pending_decisions should be cleaned up
        assert "req-exc-meta" not in router._pending_decisions

    @pytest.mark.asyncio
    async def test_stream_exception_carries_routewise_in_routing(self):
        """When stream_chat_completion fails, exception._routing contains routewise."""
        router, quota, api = _make_router_with_quota_and_api()

        async def _fail_stream(*args, **kwargs):
            raise RuntimeError("stream fail")
            yield

        quota.stream_chat_completion = _fail_stream
        api.stream_chat_completion = _fail_stream

        with pytest.raises(RuntimeError) as exc_info:
            async for _ in router.stream_chat_completion(
                "test-model",
                [{"role": "user", "content": "hi"}],
                request_id="req-stream-exc",
            ):
                pass

        exc = exc_info.value
        routing = getattr(exc, "_routing", None)
        assert routing is not None
        assert "routewise" in routing
        assert routing["routewise"]["selected_tier"] in ("quota", "api")

    @pytest.mark.asyncio
    async def test_quota_committed_always_zero(self):
        """quota_committed is 0.0 for all tiers (v_t lives in its own field)."""
        router, _conc, _quota, _api = _make_router_with_all_tiers()

        # S_C path
        meta_sc = None
        router._select_adapter("test-model", {"request_id": "req-sc-qc"})
        meta_sc = router._pending_decisions.get("req-sc-qc")
        if meta_sc:
            assert meta_sc["quota_committed"] == 0.0

        # S_Q path (disable concurrency to force quota)
        config_no_conc = RouteWiseConfig(concurrency_enabled=False)
        router2, _quota2, _api2 = _make_router_with_quota_and_api(config=config_no_conc)
        router2._select_adapter("test-model", {"request_id": "req-sq-qc"})
        meta_sq = router2._pending_decisions.get("req-sq-qc")
        if meta_sq and meta_sq["selected_tier"] == "quota":
            assert meta_sq["quota_committed"] == 0.0
            assert meta_sq["v_t"] > 0  # v_t is separate

        # S_A path
        config_sa = RouteWiseConfig(shadow_price_L_seed=1000.0)
        router3, _quota3, _api3 = _make_router_with_quota_and_api(config=config_sa)
        router3._select_adapter("test-model", {"request_id": "req-sa-qc"})
        meta_sa = router3._pending_decisions.get("req-sa-qc")
        if meta_sa:
            assert meta_sa["quota_committed"] == 0.0
