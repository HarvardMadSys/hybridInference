"""Tests for the RouteWise router: selection, resource pools, latency, hedging wiring."""

from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import json
import logging
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routing.route_table import EffectiveRoute
from routing.routers import FixedRouter, RoutingObservation
from routing.routewise.candidates import QuotaSource
from routing.routewise.config import RouteWiseConfig
from routing.routewise.decisions import ProviderReservation, RoutingDecision, RoutingTrace
from routing.routewise.envelope import EnvelopeNotCalibratedError
from routing.routewise.hedging import HedgedAdapter
from routing.routewise.quota import ProviderQuotaSnapshotStore
from routing.routewise.router import ProviderType, RouteWiseRouter
from serving.schemas_admin import ProviderQuotaResult, ProviderQuotaUsage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ADAPTER_COUNTER = itertools.count()


class _StatusError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def _unreserved_decision(
    adapter: Any,
    *,
    metadata: dict[str, Any] | None = None,
) -> RoutingDecision:
    """Build an execution decision for tests that exercise an adapter directly."""
    return RoutingDecision(
        adapter=adapter,
        reservation=ProviderReservation(),
        metadata=metadata if metadata is not None else {},
        trace=RoutingTrace(),
    )


def _make_model_config(
    model_id: str = "test-model",
    provider: str = "openai_compat",
    provider_type: str = "on_demand",
    prompt_price: str = "0.001",
    completion_price: str = "0.002",
    endpoint_id: str | None = None,
    quota_source: dict[str, str] | None = None,
    quota: dict[str, object] | None = None,
    concurrency: dict[str, object] | None = None,
    quota_pool: str | None = None,
    concurrency_pool: str | None = None,
    route_metadata: dict[str, object] | None = None,
    on_demand: bool = False,
) -> MagicMock:
    """Create a mock ModelConfig.

    Quota/concurrency routes get a default route-level resource block so most
    tests do not have to spell one out.
    """
    cfg = MagicMock()
    cfg.id = model_id
    cfg.provider = provider
    cfg.provider_type = provider_type
    cfg.endpoint_id = endpoint_id or f"{model_id}:{provider}:{next(_ADAPTER_COUNTER)}"
    # Concrete (JSON-serializable) base_url so the synthetic _routing chunk
    # emitted by FixedRouter.stream_chat_completion can be json.dumps()'d.
    cfg.base_url = f"https://{provider}.example/v1"
    cfg.pricing = {"prompt": prompt_price, "completion": completion_price}
    if quota_source is not None:
        cfg.quota_source = quota_source
    elif provider_type == "quota":
        # Quota routes require a usage truth source; default a stub one.
        cfg.quota_source = {
            "provider": "stub",
            "usage_label": "Daily requests",
            "unit": "requests",
        }
    else:
        cfg.quota_source = None
    cfg.quota = (
        quota if quota is not None else ({"limit": 10_000} if provider_type == "quota" else None)
    )
    cfg.concurrency = (
        concurrency
        if concurrency is not None
        else ({"limit": 4} if provider_type == "concurrency" else None)
    )
    cfg.quota_pool = quota_pool
    cfg.concurrency_pool = concurrency_pool
    cfg.route_metadata = route_metadata or {}
    # Explicit False, not a Mock attribute: the probe path skips on the literal
    # True, and a bare MagicMock attribute must not read as "flagged on-demand"
    # to any future truthiness check either.
    cfg.on_demand = on_demand
    return cfg


def _make_adapter(
    model_id: str = "test-model",
    provider: str = "openai_compat",
    provider_type: str = "on_demand",
    prompt_price: str = "0.001",
    completion_price: str = "0.002",
    endpoint_id: str | None = None,
    quota_source: dict[str, str] | None = None,
    quota: dict[str, object] | None = None,
    concurrency: dict[str, object] | None = None,
    quota_pool: str | None = None,
    concurrency_pool: str | None = None,
    route_metadata: dict[str, object] | None = None,
    on_demand: bool = False,
) -> MagicMock:
    """Create a mock adapter with a mock ModelConfig."""
    adapter = MagicMock()
    adapter.config = _make_model_config(
        model_id=model_id,
        provider=provider,
        provider_type=provider_type,
        prompt_price=prompt_price,
        completion_price=completion_price,
        endpoint_id=endpoint_id,
        quota_source=quota_source,
        quota=quota,
        concurrency=concurrency,
        quota_pool=quota_pool,
        concurrency_pool=concurrency_pool,
        route_metadata=route_metadata,
        on_demand=on_demand,
    )
    return adapter


class _FakeRouteTable:
    """Minimal RouteTableView double with mutable effective-weight fixtures."""

    def __init__(self) -> None:
        self._routes: dict[str, tuple[tuple[Any, float], ...]] = {}
        self.weight_overrides: dict[str, float] = {}

    def add(self, model_id: str, adapters_with_weights: list[tuple[Any, float]]) -> None:
        self._routes[model_id] = tuple(adapters_with_weights)

    def iter_effective_routes(self) -> tuple[EffectiveRoute, ...]:
        return tuple(
            EffectiveRoute(
                route_key=model_id,
                canonical_model_id=model_id,
                adapters=tuple(
                    (
                        adapter,
                        float(self.weight_overrides.get(adapter.config.endpoint_id, weight)),
                    )
                    for adapter, weight in adapters
                ),
            )
            for model_id, adapters in self._routes.items()
        )

    def canonical_id(self, model_id: str) -> str:
        return model_id


def _quota_pool(router: RouteWiseRouter):
    """Return the router's only quota pool (single-pool test fixtures)."""
    return next(iter(router.quota_pools.values()))


def _seed_quota_snapshots(router: RouteWiseRouter, *, used: float = 0.0) -> None:
    """Install a ready provider snapshot for every quota pool on the router.

    Quota pools are snapshot-backed; unit tests seed the store directly
    instead of running the async refresh loop.
    """
    from datetime import datetime, timezone

    from routing.routewise.quota import ProviderQuotaSnapshot

    now = datetime.now(timezone.utc)
    for pool in router.quota_pools.values():
        router.quota_snapshots._snapshots[pool.source] = ProviderQuotaSnapshot(
            source=pool.source,
            used=used,
            limit=float(pool.policy.limit),
            reset_at=None,
            fetched_at=now,
        )
        router.quota_snapshots._local_increments[pool.source] = 0


def _conc_pool(router: RouteWiseRouter):
    """Return the router's only concurrency pool (single-pool test fixtures)."""
    return next(iter(router.concurrency_pools.values()))


def _make_router_with_quota_and_api(
    config: RouteWiseConfig | None = None,
    prompt_price: str = "3.0",
    completion_price: str = "15.0",
    quota_limit: int = 10_000,
) -> tuple[RouteWiseRouter, MagicMock, MagicMock]:
    """Build a RouteWiseRouter with one S_Q and one S_A adapter.

    Prices are per-1M-token (matching models.yaml convention).

    Returns:
        (router, quota_adapter, api_adapter)
    """
    if config is None:
        config = RouteWiseConfig()
    quota_adapter = _make_adapter(
        provider_type="quota",
        prompt_price=prompt_price,
        completion_price=completion_price,
        endpoint_id="test-model:quota-provider",
        quota={"limit": quota_limit},
    )
    api_adapter = _make_adapter(
        provider_type="on_demand",
        prompt_price=prompt_price,
        completion_price=completion_price,
        endpoint_id="test-model:api-provider",
    )
    fr = _FakeRouteTable()
    fr.add("test-model", [(quota_adapter, 0.5), (api_adapter, 0.5)])
    router = RouteWiseRouter(route_table=fr, config=config)
    _seed_quota_snapshots(router)
    return router, quota_adapter, api_adapter


def _warm_envelope(
    router: RouteWiseRouter,
    *,
    lower: float,
    upper: float,
    model_id: str = "test-model",
    n: int = 50,
) -> None:
    """Pre-populate the cost envelope so its P10/P90 ≈ (lower, upper).

    Tests previously injected a deterministic envelope by setting
    ``shadow_price_L_seed`` / ``shadow_price_U_seed`` on ``RouteWiseConfig``,
    relying on the now-removed seed fallback in ``CostEnvelopeEstimator``.
    With the seed path gone, deterministic envelopes are constructed by
    feeding observations directly: ``n // 2`` samples at ``lower`` and
    ``n // 2`` at ``upper`` produce P10 = lower and P90 = upper for the
    default 10 / 90 percentiles.
    """

    pool = router._routewise_pool(model_id)
    half = max(n // 2, 1)
    base_ts = time.time() - 1.0
    for _ in range(half):
        router.envelope.observe(pool, lower, now=base_ts)
    for _ in range(half):
        router.envelope.observe(pool, upper, now=base_ts)


# ---------------------------------------------------------------------------
# Scaffold tests (retained from PR-2)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRouteWiseRouterScaffold:
    def test_scaffold_selects_adapter(self):
        """Router returns an adapter for a registered model."""
        adapter = _make_adapter()
        fr = _FakeRouteTable()
        fr.add("test-model", [(adapter, 1.0)])

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        selected = router._select_decision("test-model", {}).adapter
        assert selected is adapter

    async def test_open_circuit_candidate_is_excluded(self, monkeypatch):
        monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "1")
        monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")
        blocked = _make_adapter(endpoint_id="test-model:blocked", prompt_price="0.001")
        active = _make_adapter(endpoint_id="test-model:active", prompt_price="0.002")
        fr = _FakeRouteTable()
        fr.add("test-model", [(blocked, 0.5), (active, 0.5)])
        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

        with patch("serving.observability.alerts.alert_slack", new=AsyncMock()):
            router._health_registry.record_failure(
                "test-model:blocked",
                reason="upstream_502",
                exc=_StatusError(502, "bad gateway"),
            )
            await asyncio.sleep(0)

        selected = router._select_decision("test-model", {}).adapter

        assert selected is active
        assert router.get_provider_status()["test-model:blocked"]["circuit_state"] == "open"

    def test_alias_routes_use_canonical_routewise_state(self):
        """Alias requests must share RouteWise per-model state with canonical requests."""
        adapter = _make_adapter(
            model_id="minimax-m2.5",
            endpoint_id="minimax-m2.5:api-a",
        )
        fr = FixedRouter()
        fr.register_route("minimax-m2.5", [(adapter, 1.0)], aliases=["MiniMax-M2.5"])
        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

        assert sorted(router.classified) == ["minimax-m2.5"]
        assert "MiniMax-M2.5" not in router.route_candidates
        assert router.canonical_model_id("MiniMax-M2.5") == "minimax-m2.5"
        assert router._routewise_pool("MiniMax-M2.5") == "minimax-m2.5"

        selected = router._select_decision("MiniMax-M2.5", {"prompt_tokens": 1000}).adapter
        assert selected is adapter
        assert "minimax-m2.5" in router._last_lp_statuses
        assert "MiniMax-M2.5" not in router._last_lp_statuses

        router.record_observation(
            RoutingObservation(
                model_id="MiniMax-M2.5",
                endpoint_id="minimax-m2.5:api-a",
                ttft_ms=None,
                total_latency_ms=500.0,
                token_count=600,
                prompt_tokens=100,
                completion_tokens=500,
                success=True,
                strategy_metadata={"routewise": {"quota_committed": 0.0}},
            )
        )

        assert "minimax-m2.5" in router.predictor._model_states
        assert "MiniMax-M2.5" not in router.predictor._model_states
        assert "minimax-m2.5" in router.envelope._samples
        assert "MiniMax-M2.5" not in router.envelope._samples

    def test_unregistered_model_raises(self):
        """Requesting an unknown model raises ValueError."""
        fr = _FakeRouteTable()
        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        with pytest.raises(ValueError, match="no route"):
            router._select_decision("nonexistent", {})

    def test_provider_type_classification(self):
        """Adapters are correctly classified by their provider_type."""
        quota_adapter = _make_adapter(provider_type="quota")
        api_adapter = _make_adapter(provider_type="on_demand")

        fr = _FakeRouteTable()
        fr.add("test-model", [(quota_adapter, 0.5), (api_adapter, 0.5)])

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        entries = router.classified["test-model"]
        types = {s for _, _, s in entries}
        assert ProviderType.QUOTA in types
        assert ProviderType.ON_DEMAND in types

    def test_rebuild_honors_route_table_effective_weights(self):
        """Weight overrides should remove disabled endpoints from RouteWise candidates."""
        disabled_adapter = _make_adapter(
            provider_type="on_demand",
            endpoint_id="test-model:disabled-provider",
            prompt_price="0.001",
            completion_price="0.010",
        )
        active_adapter = _make_adapter(
            provider_type="on_demand",
            endpoint_id="test-model:active-provider",
            prompt_price="0.002",
            completion_price="0.020",
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(disabled_adapter, 1.0), (active_adapter, 1.0)])
        fr.weight_overrides["test-model:disabled-provider"] = 0.0

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

        assert [c.endpoint_id for c in router.route_candidates["test-model"]] == [
            "test-model:active-provider"
        ]
        selected = router._select_decision("test-model", {}).adapter
        assert selected is active_adapter

    def test_rebuild_preserves_profiles_for_remaining_endpoints(self):
        """Rebuilds should keep latency history for endpoints still in RouteWise."""
        disabled_adapter = _make_adapter(
            model_id="model-a",
            provider_type="on_demand",
            endpoint_id="model-a:disabled",
        )
        active_adapter = _make_adapter(
            model_id="model-a",
            provider_type="on_demand",
            endpoint_id="model-a:active",
        )
        other_model_adapter = _make_adapter(
            model_id="model-b",
            provider_type="on_demand",
            endpoint_id="model-b:api",
        )
        fr = _FakeRouteTable()
        fr.add("model-a", [(disabled_adapter, 1.0), (active_adapter, 1.0)])
        fr.add("model-b", [(other_model_adapter, 1.0)])

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        now = time.time()
        router._latency_profiles["model-a:disabled"].record(now, 200.0)
        router._latency_profiles["model-a:active"].record(now, 300.0)
        router._latency_profiles["model-b:api"].record(now, 400.0)
        router._latency_history_priors_ms.update(
            {
                "model-a:disabled": 200.0,
                "model-a:active": 300.0,
                "model-b:api": 400.0,
            }
        )
        active_profile = router._latency_profiles["model-a:active"]
        other_model_profile = router._latency_profiles["model-b:api"]

        fr.weight_overrides["model-a:disabled"] = 0.0
        router._rebuild_from_route_table()

        assert "model-a:disabled" not in router._latency_profiles
        assert "model-a:disabled" not in router._latency_history_priors_ms
        assert router._latency_profiles["model-a:active"] is active_profile
        assert router._latency_profiles["model-b:api"] is other_model_profile
        assert router._latency_profiles["model-a:active"].sample_count(now) == 1
        assert router._latency_profiles["model-b:api"].sample_count(now) == 1
        assert router._latency_history_priors_ms == {
            "model-a:active": 300.0,
            "model-b:api": 400.0,
        }

    def test_pd_selects_quota_when_value_high(self):
        """PD selects S_Q when estimated API cost exceeds shadow price.

        With per-1M prices (3.0 prompt, 15.0 completion) and cold-start
        output prediction (500), v_t = 15/1M * 500 = 0.0075 > L_seed=0.001.
        """
        quota_adapter = _make_adapter(
            provider_type="quota",
            prompt_price="3.0",
            completion_price="15.0",
            endpoint_id="test-model:quota-provider",
        )
        api_adapter = _make_adapter(
            provider_type="on_demand",
            prompt_price="3.0",
            completion_price="15.0",
            endpoint_id="test-model:api-provider",
        )

        fr = _FakeRouteTable()
        fr.add("test-model", [(api_adapter, 0.5), (quota_adapter, 0.5)])

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        _seed_quota_snapshots(router)
        _warm_envelope(router, lower=0.0000001, upper=0.001)
        selected = router._select_decision("test-model", {}).adapter
        assert selected is quota_adapter

    def test_scaffold_selects_cheapest_api_by_request_cost(self):
        """Cheapest API is selected per-request, not by fixed unit sum.

        With cold-start output prediction (500) and 0 prompt tokens, the
        selection depends only on completion price.  Provider B (0.003/1M)
        beats Provider A (0.010/1M) on completion price.
        """
        # Provider A: cheap prompt but expensive completion
        provider_a = _make_adapter(
            provider_type="on_demand", prompt_price="0.002", completion_price="0.010"
        )
        # Provider B: balanced pricing
        provider_b = _make_adapter(
            provider_type="on_demand", prompt_price="0.003", completion_price="0.003"
        )

        fr = _FakeRouteTable()
        fr.add("test-model", [(provider_a, 0.5), (provider_b, 0.5)])

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        selected = router._select_decision("test-model", {}).adapter
        assert selected is provider_b

    def test_per_request_cheapest_varies_by_prompt_ratio(self):
        """The cheapest S_A adapter can change with prompt/output ratio.

        Provider A: prompt=0.5/1M, completion=20.0/1M  (cheap input)
        Provider B: prompt=5.0/1M, completion=5.0/1M   (balanced)

        With large prompt_tokens and small predicted output, A is cheaper.
        With small prompt_tokens and large predicted output, B is cheaper.
        """
        provider_a = _make_adapter(
            provider_type="on_demand",
            prompt_price="0.5",
            completion_price="20.0",
        )
        provider_b = _make_adapter(
            provider_type="on_demand",
            prompt_price="5.0",
            completion_price="5.0",
        )

        fr = _FakeRouteTable()
        fr.add("test-model", [(provider_a, 0.5), (provider_b, 0.5)])

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

        # Warm predictor so predicted output is ~100 (small output).
        for _ in range(25):
            router.predictor.update("test-model", 100)

        # Large prompt (10000), small output (~100).
        # A: 0.5/1M * 10000 + 20.0/1M * 100 = 0.005 + 0.002 = 0.007
        # B: 5.0/1M * 10000 + 5.0/1M * 100  = 0.050 + 0.0005 = 0.0505
        # A is cheaper.
        selected = router._select_decision("test-model", {"prompt_tokens": 10000}).adapter
        assert selected is provider_a

    def test_unknown_provider_type_raises(self):
        """Unknown provider_type values are rejected."""
        adapter = _make_adapter(provider_type="unknown_tier")
        fr = _FakeRouteTable()
        fr.add("test-model", [(adapter, 1.0)])

        with pytest.raises(ValueError, match="provider_type must be one of"):
            RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

    def test_subscription_only_route_uses_reference_api_price_for_value(self):
        """Routes without S_A can still price requests with reference_api_price."""
        quota_adapter = _make_adapter(provider_type="quota")
        fr = _FakeRouteTable()
        fr.add("test-model", [(quota_adapter, 1.0)])

        router = RouteWiseRouter(
            route_table=fr,
            config=RouteWiseConfig(reference_api_price={"prompt": "2.0", "completion": "4.0"}),
        )

        assert router._estimate_value("test-model", 1000) == pytest.approx(0.004048)

    def test_quota_source_without_snapshot_masks_quota_candidate(self):
        """Provider-backed S_Q is not feasible until a quota snapshot exists."""
        source = {
            "provider": "chutes",
            "usage_label": "Daily requests",
            "unit": "requests",
        }
        quota_adapter = _make_adapter(
            provider_type="quota",
            quota_source=source,
            endpoint_id="test-model:quota-provider",
        )
        api_adapter = _make_adapter(
            provider_type="on_demand",
            endpoint_id="test-model:api-provider",
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(quota_adapter, 0.5), (api_adapter, 0.5)])

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

        selected = router._select_decision("test-model", {"prompt_tokens": 1000}).adapter

        assert selected is api_adapter
        assert router._quota_sources() == [
            QuotaSource(provider="chutes", usage_label="Daily requests", unit="requests")
        ]

    def test_upstream_override_quota_uses_local_limit_fallback(self):
        """Upstream-overridden S_Q uses quota.limit as local request-count state."""
        source = {
            "provider": "chutes",
            "usage_label": "Daily requests",
            "unit": "requests",
        }
        quota_adapter = _make_adapter(
            provider_type="quota",
            quota_source=source,
            quota={"limit": 5000},
            endpoint_id="test-model:quota-provider",
            route_metadata={
                "route_provider": "chutes",
                "upstream_provider": "openrouter",
                "provider_type": "quota",
            },
        )
        api_adapter = _make_adapter(
            provider_type="on_demand",
            prompt_price="3.0",
            completion_price="15.0",
            endpoint_id="test-model:api-provider",
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(quota_adapter, 0.5), (api_adapter, 0.5)])

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        _warm_envelope(router, lower=0.0000001, upper=0.001)

        decision = router._select_decision(
            "test-model",
            {"prompt_tokens": 1000, "request_id": "req-local-fallback"},
        )

        assert decision.adapter is quota_adapter
        assert router._quota_sources() == []
        source_obj = QuotaSource(provider="chutes", usage_label="Daily requests", unit="requests")
        assert router.quota_snapshots.get(source_obj) is None
        pool_id = next(iter(router.quota_pools))
        pool = _quota_pool(router)
        assert pool.source == QuotaSource(
            provider="local",
            usage_label=f"routewise:{pool_id}",
            unit="requests",
        )
        snapshot = router.quota_snapshots.get(pool.source)
        assert snapshot is not None
        assert snapshot.limit == 5000
        assert snapshot.remaining == 4999
        assert decision.metadata["quota_remaining"] == 4999

    def test_upstream_override_quota_fallback_does_not_mask_shared_source(self):
        """A local fallback pool must not suppress real refresh for another pool."""
        source = {
            "provider": "chutes",
            "usage_label": "Daily requests",
            "unit": "requests",
        }
        override_adapter = _make_adapter(
            provider_type="quota",
            quota_source=source,
            quota={"limit": 5000},
            quota_pool="override-pool",
            endpoint_id="test-model:override-quota",
            route_metadata={
                "route_provider": "chutes",
                "upstream_provider": "openrouter",
                "provider_type": "quota",
            },
        )
        normal_adapter = _make_adapter(
            model_id="other-model",
            provider_type="quota",
            quota_source=source,
            quota={"limit": 5000},
            quota_pool="normal-pool",
            endpoint_id="other-model:normal-quota",
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(override_adapter, 1.0)])
        fr.add("other-model", [(normal_adapter, 1.0)])

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        source_obj = QuotaSource(provider="chutes", usage_label="Daily requests", unit="requests")

        assert router.quota_pools["override-pool"].source == QuotaSource(
            provider="local",
            usage_label="routewise:override-pool",
            unit="requests",
        )
        assert router.quota_pools["normal-pool"].source == source_obj
        assert router._quota_sources() == [source_obj]

    @pytest.mark.asyncio
    async def test_quota_source_snapshot_enables_quota_candidate(self):
        """Provider-backed S_Q uses the fetched Chutes quota fraction."""
        source = {
            "provider": "chutes",
            "usage_label": "Daily requests",
            "unit": "requests",
        }
        quota_adapter = _make_adapter(
            provider_type="quota",
            quota_source=source,
            endpoint_id="test-model:quota-provider",
        )
        api_adapter = _make_adapter(
            provider_type="on_demand",
            prompt_price="3.0",
            completion_price="15.0",
            endpoint_id="test-model:api-provider",
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(quota_adapter, 0.5), (api_adapter, 0.5)])

        async def fake_fetch_chutes() -> list[ProviderQuotaResult]:
            return [
                ProviderQuotaResult(
                    name="chutes",
                    display_name="Chutes",
                    key_configured=True,
                    key_masked="***",
                    fetched_at=None,
                    ok=True,
                    error=None,
                    usages=[
                        ProviderQuotaUsage(
                            label="Daily requests",
                            used=10.0,
                            limit=100.0,
                            unit="requests",
                            reset_at=None,
                        )
                    ],
                )
            ]

        config = RouteWiseConfig()
        router = RouteWiseRouter(route_table=fr, config=config)
        _warm_envelope(router, lower=0.0000001, upper=0.001)
        router.quota_snapshots = ProviderQuotaSnapshotStore(fetchers={"chutes": fake_fetch_chutes})
        # Snapshot pools hold a store reference; rebuild after swapping it.
        router._build_resource_pools()
        await router.refresh_quota_snapshots_once()

        decision = router._select_decision(
            "test-model",
            {"prompt_tokens": 1000, "request_id": "req-with-snapshot"},
        )

        assert decision.adapter is quota_adapter
        snapshot = router.quota_snapshots.get(
            QuotaSource(provider="chutes", usage_label="Daily requests", unit="requests")
        )
        assert snapshot is not None
        assert snapshot.remaining == 89
        assert decision.metadata["quota_remaining"] == 89
        assert decision.metadata["quota_source"] == source

    def test_stateful_providers_raise_when_worker_count_is_multi_process(self, monkeypatch):
        """S_Q/S_C are process-local and guarded in multi-worker deployments."""
        monkeypatch.setenv("WEB_CONCURRENCY", "2")

        quota_adapter = _make_adapter(provider_type="quota")
        api_adapter = _make_adapter(provider_type="on_demand")
        fr = _FakeRouteTable()
        fr.add("test-model", [(quota_adapter, 0.5), (api_adapter, 0.5)])

        with pytest.raises(RuntimeError, match="process-local"):
            RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

    def test_multi_worker_guard_does_not_block_api_only_routes(self, monkeypatch):
        """API-only RouteWise routes remain safe with multiple workers."""
        monkeypatch.setenv("WEB_CONCURRENCY", "2")

        api_adapter = _make_adapter(provider_type="on_demand")
        fr = _FakeRouteTable()
        fr.add("test-model", [(api_adapter, 1.0)])

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        assert router._select_decision("test-model", {}).adapter is api_adapter

    def test_attach_route_table_rebuilds_derived_route_state(self):
        """Rebinding resets derived state owned by the route table."""
        adapter = _make_adapter()
        fr = _FakeRouteTable()
        fr.add("test-model", [(adapter, 1.0)])

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

        replacement = _FakeRouteTable()
        replacement.add("test-model", [(adapter, 1.0)])

        router.attach_route_table(replacement)

        assert router.route_table is replacement
        assert set(router.classified) == {"test-model"}

    def test_legacy_fixed_router_keyword_delegates_to_route_table(self):
        route_table = _FakeRouteTable()
        route_table.add("test-model", [(_make_adapter(), 1.0)])

        router = RouteWiseRouter(fixed_router=route_table, config=RouteWiseConfig())

        assert router.route_table is route_table
        assert router.fixed_router is route_table

    def test_legacy_fixed_router_attribute_assignment_updates_route_table(self):
        router = RouteWiseRouter(config=RouteWiseConfig())
        route_table = _FakeRouteTable()

        router.fixed_router = route_table

        assert router.route_table is route_table

    def test_legacy_attach_and_rebuild_shims_delegate_to_route_table(self, monkeypatch):
        first = _FakeRouteTable()
        first.add("test-model", [(_make_adapter(), 1.0)])
        replacement = _FakeRouteTable()
        replacement.add("test-model", [(_make_adapter(), 1.0)])
        router = RouteWiseRouter(route_table=first, config=RouteWiseConfig())

        router.attach_fixed_router(replacement)
        rebuild = MagicMock(wraps=router._rebuild_from_route_table)
        monkeypatch.setattr(router, "_rebuild_from_route_table", rebuild)

        router._rebuild_from_fixed_router()

        assert router.route_table is replacement
        rebuild.assert_called_once_with()

    def test_public_route_table_refresh_rebuilds_under_commit_lock(self, monkeypatch):
        router = RouteWiseRouter(config=RouteWiseConfig())

        class _RecordingLock:
            def __init__(self) -> None:
                self.depth = 0

            def __enter__(self) -> _RecordingLock:
                self.depth += 1
                return self

            def __exit__(self, *_exc_info: object) -> None:
                self.depth -= 1

        commit_lock = _RecordingLock()
        rebuild_depths: list[int] = []

        def _record_rebuild() -> None:
            rebuild_depths.append(commit_lock.depth)

        router._route_commit_lock = commit_lock
        monkeypatch.setattr(router, "_rebuild_from_route_table", _record_rebuild)

        router.refresh_route_table()

        assert rebuild_depths == [1]
        assert commit_lock.depth == 0

    def test_constructor_does_not_dispatch_to_overridden_public_refresh(self):
        route_table = _FakeRouteTable()
        route_table.add("test-model", [(_make_adapter(), 1.0)])

        class _Subclass(RouteWiseRouter):
            def refresh_route_table(self) -> None:
                raise AssertionError("subclass refresh called before initialization completed")

        router = _Subclass(route_table=route_table, config=RouteWiseConfig())

        assert set(router.classified) == {"test-model"}

    def test_route_table_and_legacy_fixed_router_are_mutually_exclusive(self):
        route_table = _FakeRouteTable()

        with pytest.raises(TypeError, match="cannot both be provided"):
            RouteWiseRouter(
                route_table=route_table,
                fixed_router=route_table,
                config=RouteWiseConfig(),
            )

    def test_concurrency_adapter_skipped_when_pool_exhausted(self):
        """S_C adapter is not selected when its pool has no free slots."""
        conc = _make_adapter(provider_type="concurrency", concurrency={"limit": 1})
        fr = _FakeRouteTable()
        fr.add("test-model", [(conc, 1.0)])

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        assert _conc_pool(router).try_acquire() is True
        decision = router._select_decision("test-model", {})
        assert decision is None

    def test_concurrency_adapter_selected_with_route_policy(self):
        """S_C adapter is returned when its route declares a concurrency block."""
        conc = _make_adapter(provider_type="concurrency")
        fr = _FakeRouteTable()
        fr.add("test-model", [(conc, 1.0)])

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        selected = router._select_decision("test-model", {}).adapter
        assert selected is conc


# ---------------------------------------------------------------------------
# Quota / API decision logic tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRouteWiseQuotaDecision:
    def test_routes_to_quota_when_value_exceeds_threshold(self):
        """When v_t >= theta_Q and quota remains, RouteWise selects S_Q."""
        router, quota_adapter, _api_adapter = _make_router_with_quota_and_api(
            prompt_price="3.0", completion_price="15.0"
        )
        _warm_envelope(router, lower=0.0000001, upper=0.001)
        # Warm the predictor so v_t is meaningful.
        for _ in range(25):
            router.predictor.update("test-model", 500)

        initial_remaining = _quota_pool(router).remaining
        selected = router._select_decision("test-model", {"prompt_tokens": 1000}).adapter
        assert selected is quota_adapter
        # Selection-commit: quota consumed at selection time.
        assert _quota_pool(router).remaining < initial_remaining

    def test_routes_to_api_when_value_below_threshold(self):
        """When v_t < theta_Q, RouteWise selects the cheapest S_A adapter."""
        # Very high shadow price bounds so theta_Q >> v_t.
        router, _quota_adapter, api_adapter = _make_router_with_quota_and_api(
            prompt_price="3.0", completion_price="15.0"
        )
        _warm_envelope(router, lower=1000.0, upper=10000.0)
        for _ in range(25):
            router.predictor.update("test-model", 500)

        initial_remaining = _quota_pool(router).remaining
        selected = router._select_decision("test-model", {"prompt_tokens": 1000}).adapter
        assert selected is api_adapter
        # No selection-commit: quota unchanged.
        assert _quota_pool(router).remaining == initial_remaining

    def test_no_quota_adapter_always_selects_api(self):
        """Models with only S_A adapters never route to S_Q."""
        api_only = _make_adapter(provider_type="on_demand")
        fr = _FakeRouteTable()
        fr.add("test-model", [(api_only, 1.0)])

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

        selected = router._select_decision("test-model", {"prompt_tokens": 1000}).adapter
        assert selected is api_only

    def test_quota_exhausted_routes_to_api(self):
        """When quota is exhausted, PD routes to S_A even if value is high."""
        router, _quota_adapter, api_adapter = _make_router_with_quota_and_api(quota_limit=100)
        _warm_envelope(router, lower=0.0000001, upper=0.001)
        for _ in range(25):
            router.predictor.update("test-model", 500)

        # Exhaust the quota (100 requests).
        for _ in range(100):
            _quota_pool(router).consume()
        assert _quota_pool(router).remaining == 0

        selected = router._select_decision("test-model", {"prompt_tokens": 1000}).adapter
        assert selected is api_adapter

    def test_context_prompt_tokens_used(self):
        """Explicit prompt_tokens from context flows into value estimation."""
        router, _, _ = _make_router_with_quota_and_api()

        for _ in range(25):
            router.predictor.update("test-model", 500)

        v_small = router._estimate_value("test-model", 100)
        v_large = router._estimate_value("test-model", 10000)
        assert v_large > v_small

    def test_prompt_tokens_estimated_from_messages(self):
        """When prompt_tokens is absent, tokens are estimated from messages."""
        router, quota_adapter, _api_adapter = _make_router_with_quota_and_api()
        _warm_envelope(router, lower=0.0000001, upper=0.001)
        for _ in range(25):
            router.predictor.update("test-model", 500)

        # Build messages with ~4000 chars (~1000 tokens).
        long_msg = "x" * 4000
        context_with_messages = {
            "messages": [{"role": "user", "content": long_msg}],
        }
        # Should still route correctly (not silently use 0 prompt_tokens).
        initial_remaining = _quota_pool(router).remaining
        selected = router._select_decision("test-model", context_with_messages).adapter
        assert selected is quota_adapter
        # Quota consumed: one request slot.
        consumed = initial_remaining - _quota_pool(router).remaining
        assert consumed == 1

    def test_selection_commit_consumes_quota_at_selection(self):
        """Quota is consumed at selection time, not deferred to observation."""
        router, quota_adapter, _api_adapter = _make_router_with_quota_and_api()
        _warm_envelope(router, lower=0.0000001, upper=0.001)
        for _ in range(25):
            router.predictor.update("test-model", 500)

        before = _quota_pool(router).remaining
        selected = router._select_decision("test-model", {"prompt_tokens": 200}).adapter
        assert selected is quota_adapter

        after = _quota_pool(router).remaining
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
            strategy_metadata={"routewise": {"quota_committed": 0.0}},
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
        router, _quota_adapter, _api_adapter = _make_router_with_quota_and_api(quota_limit=5000)

        initial_remaining = _quota_pool(router).remaining

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
            strategy_metadata={"routewise": {"quota_committed": 0.0}},
        )
        router.record_observation(obs)

        # Quota must NOT change in record_observation.
        assert _quota_pool(router).remaining == initial_remaining

    def test_record_observation_api_does_not_consume_quota(self):
        """Quota is untouched for S_A routed observations."""
        router, _quota_adapter, _api_adapter = _make_router_with_quota_and_api(quota_limit=5000)

        initial_remaining = _quota_pool(router).remaining

        obs = RoutingObservation(
            model_id="test-model",
            endpoint_id="test-model:api-provider",
            ttft_ms=50.0,
            total_latency_ms=200.0,
            token_count=600,
            prompt_tokens=100,
            completion_tokens=500,
            success=True,
            strategy_metadata={"routewise": {"quota_committed": 0.0}},
        )
        router.record_observation(obs)

        assert _quota_pool(router).remaining == initial_remaining

    def test_record_observation_does_not_raise(self):
        """record_observation never raises, even with edge-case data."""
        fr = _FakeRouteTable()
        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        obs = RoutingObservation(
            model_id="unknown-model",
            endpoint_id="unknown:provider",
            ttft_ms=None,
            total_latency_ms=0.0,
            token_count=0,
            prompt_tokens=0,
            completion_tokens=0,
            success=False,
            strategy_metadata={"routewise": {"quota_committed": 0.0}},
        )
        router.record_observation(obs)  # Should not raise.


# ---------------------------------------------------------------------------
# Latency-aware provider selection tests
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
        provider_type="on_demand",
        prompt_price="3.0",
        completion_price="15.0",
        endpoint_id="test-model:api-a",
    )
    api_b = _make_adapter(
        provider_type="on_demand",
        prompt_price="4.0",
        completion_price="20.0",
        endpoint_id="test-model:api-b",
    )
    fr = _FakeRouteTable()
    fr.add("test-model", [(api_a, 0.5), (api_b, 0.5)])
    router = RouteWiseRouter(route_table=fr, config=config)
    return router, api_a, api_b


@pytest.mark.unit
class TestRouteWiseLayer2:
    def test_layer2_uses_lp_when_warmed(self):
        """After profile warmup, LP path is used for S_A selection."""
        config = RouteWiseConfig(
            latency_min_samples=5,
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
        selected = router._select_decision("test-model", {"prompt_tokens": 1000}).adapter
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
        selected = router._select_decision("test-model", {"prompt_tokens": 1000}).adapter
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
            strategy_metadata={"routewise": {"quota_committed": 0.0}},
        )
        router.record_observation(obs)

        # Profile should have 1 sample.
        profile = router._latency_profiles["test-model:api-a"]
        now = time.time()
        assert profile.sample_count(now) == 1

    def test_record_observation_uses_total_latency_when_ttft_missing(self):
        """Non-streaming successes still feed the latency profile."""
        config = RouteWiseConfig()
        router, _api_a, _api_b = _make_router_with_two_api(config)

        obs = RoutingObservation(
            model_id="test-model",
            endpoint_id="test-model:api-a",
            ttft_ms=None,
            total_latency_ms=500.0,
            token_count=600,
            prompt_tokens=100,
            completion_tokens=500,
            success=True,
            strategy_metadata={"routewise": {"quota_committed": 0.0}},
        )
        router.record_observation(obs)

        profile = router._latency_profiles["test-model:api-a"]
        now = time.time()
        assert profile.sample_count(now) == 1
        assert profile.mean_with_errors_sec(
            now,
            error_penalty_ms=60_000.0,
        ) == pytest.approx(0.5)

    def test_single_api_uses_lp_single_provider_solution(self):
        """Single S_A provider returns a degenerate single-provider LP solution."""
        api_only = _make_adapter(
            provider_type="on_demand",
            prompt_price="3.0",
            completion_price="15.0",
            endpoint_id="test-model:api-only",
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(api_only, 1.0)])

        config = RouteWiseConfig(latency_min_samples=1)
        router = RouteWiseRouter(route_table=fr, config=config)

        # Warm predictor.
        for _ in range(25):
            router.predictor.update("test-model", 500)

        selected = router._select_decision("test-model", {"prompt_tokens": 1000}).adapter
        assert selected is api_only
        assert router._last_lp_statuses.get("test-model") == "single_provider"

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
            strategy_metadata={"routewise": {"quota_committed": 0.0}},
        )
        router.record_observation(obs)

        profile = router._latency_profiles["test-model:api-a"]
        now = time.time()
        assert profile.error_rate(now) > 0

    def test_latency_mean_includes_error_penalty(self):
        """Body LP latency matches real-eval mean-with-errors semantics."""
        config = RouteWiseConfig(latency_min_samples=10, latency_unprofiled_ttft_ms=5000.0)
        router, _api_a, _api_b = _make_router_with_two_api(config)
        profile = router._latency_profiles["test-model:api-a"]
        now = time.time()

        profile.record(now, 100.0)
        profile.record(now, -1.0, error_type="timeout")

        assert router._mean_ttft_sec("test-model:api-a", now) == pytest.approx(30.05)

    def test_latency_history_prior_used_when_live_window_empty(self):
        """Cold endpoints use successful history before the configured fallback."""
        config = RouteWiseConfig(
            latency_window_sec=10.0,
            latency_history_prior_window_sec=3600.0,
            latency_unprofiled_ttft_ms=5000.0,
        )
        router, _api_a, _api_b = _make_router_with_two_api(config)

        counts = router.bootstrap_from_log_rows(
            [
                {
                    "timestamp": 100.0,
                    "model_id": "test-model",
                    "endpoint_id": "test-model:api-a",
                    "ttft_ms": 250,
                    "latency_ms": 700,
                    "status_code": 200,
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                }
            ],
            include_envelope=False,
        )

        assert counts["latency_prior_samples"] == 1
        assert router._mean_ttft_sec("test-model:api-a", 1000.0) == pytest.approx(0.25)
        assert router._latency_estimate("test-model:api-a", 1000.0)[1] == "history_prior"
        fallback_value, fallback_source = router._latency_estimate("test-model:api-b", 1000.0)
        assert fallback_value == pytest.approx(5.0)
        assert fallback_source == "fallback"

    async def test_probe_success_updates_profile_and_store(self):
        """Active probe successes warm the latency profile and persist samples."""
        router, api_a, _api_b = _make_router_with_two_api()

        async def stream(_messages, **_params):
            yield 'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'

        api_a.stream_chat_completion = stream
        router._endpoint_adapter = {"test-model:api-a": api_a}
        router._endpoint_models = {"test-model:api-a": {"test-model"}}
        store = MagicMock()
        store.insert_routewise_probe_sample = AsyncMock()
        router.attach_operational_store(store)

        results = await router.run_probe_once(endpoint_id="test-model:api-a", idle_only=False)

        assert len(results) == 1
        assert results[0].ok is True
        assert results[0].ttft_ms is not None
        assert router._latency_profiles["test-model:api-a"].sample_count(time.time()) == 1
        store.insert_routewise_probe_sample.assert_awaited_once()

    async def test_probe_success_counts_reasoning_delta(self):
        """Active probes match RouteWise real-eval TTFT semantics for reasoning models."""
        router, api_a, _api_b = _make_router_with_two_api()
        calls: list[dict[str, Any]] = []

        async def stream(messages, **params):
            calls.append({"messages": messages, "params": params})
            yield 'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n'
            yield 'data: {"choices":[{"delta":{"reasoning":"thinking"}}]}\n\n'

        api_a.stream_chat_completion = stream
        router._endpoint_adapter = {"test-model:api-a": api_a}
        router._endpoint_models = {"test-model:api-a": {"test-model"}}

        results = await router.run_probe_once(endpoint_id="test-model:api-a", idle_only=False)

        assert len(results) == 1
        assert results[0].ok is True
        assert results[0].ttft_ms is not None
        assert calls == [
            {
                "messages": [{"role": "user", "content": "Write a one-sentence greeting."}],
                "params": {"max_tokens": 8, "temperature": 0},
            }
        ]

    async def test_probe_failure_records_error_penalty(self):
        """Active probe failures use the same 60s error penalty as traffic."""
        router, api_a, _api_b = _make_router_with_two_api()

        async def stream(_messages, **_params):
            raise TimeoutError("probe timed out")
            yield ""  # pragma: no cover

        api_a.stream_chat_completion = stream
        router._endpoint_adapter = {"test-model:api-a": api_a}
        router._endpoint_models = {"test-model:api-a": {"test-model"}}

        results = await router.run_probe_once(endpoint_id="test-model:api-a", idle_only=False)

        assert len(results) == 1
        assert results[0].ok is False
        assert results[0].error == "probe timed out"
        assert router._mean_ttft_sec("test-model:api-a", time.time()) == pytest.approx(60.0)

    # A catalog-`on_demand` endpoint (lazily loaded on shared GPUs) idles by
    # construction, so idle_only selects it every cycle: the probe traffic
    # itself pins the GPUs and books error penalties for the losers of the GPU
    # race. The prober must leave these endpoints alone entirely.
    async def test_probe_skips_catalog_on_demand_endpoint(self):
        """Background probing never targets an endpoint flagged on_demand."""
        router, api_a, api_b = _make_router_with_two_api()

        async def stream(_messages, **_params):
            yield 'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'

        api_a.stream_chat_completion = stream
        api_b.config.on_demand = True
        api_b.stream_chat_completion = AsyncMock(side_effect=AssertionError("should not probe"))
        router._endpoint_adapter = {
            "test-model:api-a": api_a,
            "test-model:api-b": api_b,
        }
        router._endpoint_models = {
            "test-model:api-a": {"test-model"},
            "test-model:api-b": {"test-model"},
        }

        results = await router.run_probe_once(idle_only=False)

        assert [result.endpoint_id for result in results] == ["test-model:api-a"]
        api_b.stream_chat_completion.assert_not_called()

    # A manual probe cold-starts real GPUs all the same, so naming the endpoint
    # explicitly is not an override.
    async def test_probe_skips_on_demand_even_when_explicitly_targeted(self):
        """run_probe_once(endpoint_id=...) still refuses an on_demand endpoint."""
        router, api_a, _api_b = _make_router_with_two_api()
        api_a.config.on_demand = True
        api_a.stream_chat_completion = AsyncMock(side_effect=AssertionError("should not probe"))
        router._endpoint_adapter = {"test-model:api-a": api_a}
        router._endpoint_models = {"test-model:api-a": {"test-model"}}

        results = await router.run_probe_once(endpoint_id="test-model:api-a", idle_only=False)

        assert results == []
        api_a.stream_chat_completion.assert_not_called()

    # Strict identity: the flag crosses config boundaries, and anything but the
    # literal True (a string, a Mock attribute) must leave the endpoint probed.
    async def test_probe_keeps_endpoint_when_on_demand_flag_not_literal_true(self):
        """A non-boolean on_demand value does not silently disable probing."""
        router, api_a, _api_b = _make_router_with_two_api()

        async def stream(_messages, **_params):
            yield 'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'

        api_a.config.on_demand = "true"
        api_a.stream_chat_completion = stream
        router._endpoint_adapter = {"test-model:api-a": api_a}
        router._endpoint_models = {"test-model:api-a": {"test-model"}}

        results = await router.run_probe_once(idle_only=False)

        assert [result.endpoint_id for result in results] == ["test-model:api-a"]
        assert results[0].ok is True

    async def test_probe_cycle_without_lease_syncs_shared_samples_only(self):
        """A non-leader worker should consume DB probe samples without probing upstream."""
        router, api_a, _api_b = _make_router_with_two_api()
        api_a.stream_chat_completion = AsyncMock(side_effect=AssertionError("should not probe"))
        checked_at = dt.datetime.now(dt.timezone.utc)
        store = MagicMock()
        store.list_routewise_probe_samples = AsyncMock(
            return_value=[
                {
                    "id": 7,
                    "model_id": "test-model",
                    "endpoint_id": "test-model:api-a",
                    "ttft_ms": 123.0,
                    "ok": True,
                    "error": None,
                    "checked_at": checked_at,
                }
            ]
        )
        store.try_acquire_routewise_probe_lease = AsyncMock(return_value=False)
        router.attach_operational_store(store)

        results = await router._run_probe_cycle()

        assert results == []
        assert router._last_probe_results == []
        assert router._latency_profiles["test-model:api-a"].sample_count(time.time()) == 1
        store.list_routewise_probe_samples.assert_awaited_once_with(after_id=0, limit=10_000)
        store.try_acquire_routewise_probe_lease.assert_awaited_once()

    async def test_probe_cycle_leader_tracks_self_sample_ids(self):
        """A leader records its own probe immediately and skips DB replay of the same row."""
        router, api_a, _api_b = _make_router_with_two_api()

        async def stream(_messages, **_params):
            yield 'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'

        api_a.stream_chat_completion = stream
        router._endpoint_adapter = {"test-model:api-a": api_a}
        router._endpoint_models = {"test-model:api-a": {"test-model"}}
        store = MagicMock()
        store.list_routewise_probe_samples = AsyncMock(return_value=[])
        store.try_acquire_routewise_probe_lease = AsyncMock(return_value=True)
        store.insert_routewise_probe_sample = AsyncMock(return_value=9)
        router.attach_operational_store(store)

        results = await router._run_probe_cycle()

        assert len(results) == 1
        assert router._latency_profiles["test-model:api-a"].sample_count(time.time()) == 1

        store.list_routewise_probe_samples = AsyncMock(
            return_value=[
                {
                    "id": 9,
                    "model_id": "test-model",
                    "endpoint_id": "test-model:api-a",
                    "ttft_ms": results[0].ttft_ms,
                    "ok": True,
                    "error": None,
                    "checked_at": dt.datetime.now(dt.timezone.utc),
                }
            ]
        )

        counts = await router.sync_probe_samples_once()

        assert counts["rows"] == 1
        assert counts["applied_rows"] == 0
        assert router._probe_sample_watermark_id == 9
        assert router._latency_profiles["test-model:api-a"].sample_count(time.time()) == 1

    async def test_sync_probe_samples_applies_external_rows_once(self):
        """Persisted probe samples from another worker update this worker's profile once."""
        router, _api_a, _api_b = _make_router_with_two_api()
        checked_at = dt.datetime.now(dt.timezone.utc)
        store = MagicMock()
        store.list_routewise_probe_samples = AsyncMock(
            return_value=[
                {
                    "id": 3,
                    "model_id": "test-model",
                    "endpoint_id": "test-model:api-a",
                    "ttft_ms": 250.0,
                    "ok": True,
                    "error": None,
                    "checked_at": checked_at,
                }
            ]
        )
        router.attach_operational_store(store)

        counts = await router.sync_probe_samples_once()

        assert counts["rows"] == 1
        assert counts["applied_rows"] == 1
        assert counts["latency_events"] == 1
        assert router._probe_sample_watermark_id == 3
        assert router._mean_ttft_sec("test-model:api-a", time.time()) == pytest.approx(0.25)

    def test_bootstrap_from_log_rows_warms_latency_and_envelope(self):
        """Startup history replay warms profiles with the same online semantics."""
        router, _api_a, _api_b = _make_router_with_two_api()

        counts = router.bootstrap_from_log_rows(
            [
                {
                    "timestamp": 100.0,
                    "model_id": "test-model",
                    "endpoint_id": "test-model:api-a",
                    "ttft_ms": 200,
                    "latency_ms": 500,
                    "status_code": 200,
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "failed_attempts": [
                        {
                            "endpoint_id": "test-model:api-b",
                            "error_type": "timeout",
                            "error": "deadline",
                        },
                        {
                            "endpoint_id": "test-model:api-b",
                            "error_type": "timeout",
                            "error": "deadline",
                        },
                    ],
                },
                {
                    "timestamp": 101.0,
                    "model_id": "test-model",
                    "endpoint_id": "test-model:api-a",
                    "ttft_ms": None,
                    "latency_ms": 800,
                    "status_code": 500,
                    "error": "upstream failed",
                    "prompt_tokens": 1000,
                    "completion_tokens": 0,
                },
            ]
        )

        assert counts == {
            "rows": 2,
            "latency_events": 2,
            "failed_attempts": 1,
            "envelope_samples": 1,
            "latency_prior_samples": 1,
        }
        profile_a = router._latency_profiles["test-model:api-a"]
        profile_b = router._latency_profiles["test-model:api-b"]
        assert profile_a.total_count(101.0) == 2
        assert profile_a.error_rate(101.0) == pytest.approx(0.5)
        assert profile_a.mean_with_errors_sec(
            101.0,
            error_penalty_ms=60_000.0,
        ) == pytest.approx(30.1)
        assert profile_b.total_count(101.0) == 1
        assert profile_b.error_rate(101.0) == pytest.approx(1.0)
        assert router.envelope.sample_count("test-model", now=101.0) == 1

    def test_multi_model_layer2_isolation(self):
        """Two models sharing one RouteWiseRouter have independent LP state.

        Model A's LP solve must NOT pollute model B's LP weights, and
        model B's LP interval check must be independent of model A.
        """
        config = RouteWiseConfig(
            latency_min_samples=5,
            latency_slo_sec=2.0,
        )

        # Two models, each with two S_A endpoints.
        a1 = _make_adapter(
            provider_type="on_demand",
            prompt_price="3.0",
            completion_price="15.0",
            endpoint_id="model-a:ep1",
        )
        a2 = _make_adapter(
            provider_type="on_demand",
            prompt_price="4.0",
            completion_price="20.0",
            endpoint_id="model-a:ep2",
        )
        b1 = _make_adapter(
            provider_type="on_demand",
            prompt_price="5.0",
            completion_price="10.0",
            endpoint_id="model-b:ep1",
        )
        b2 = _make_adapter(
            provider_type="on_demand",
            prompt_price="6.0",
            completion_price="12.0",
            endpoint_id="model-b:ep2",
        )

        fr = _FakeRouteTable()
        fr.add("model-a", [(a1, 0.5), (a2, 0.5)])
        fr.add("model-b", [(b1, 0.5), (b2, 0.5)])

        router = RouteWiseRouter(route_table=fr, config=config)

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
        sel_a = router._select_decision("model-a", {"prompt_tokens": 1000}).adapter
        assert sel_a is not None
        assert sel_a in (a1, a2), "Model A must select from its own endpoints"

        # Route model B.
        sel_b = router._select_decision("model-b", {"prompt_tokens": 1000}).adapter
        assert sel_b is not None
        assert sel_b in (b1, b2), "Model B must select from its own endpoints"

        # LP statuses are independent per model.
        assert router._last_lp_statuses.get("model-a") is not None
        assert router._last_lp_statuses.get("model-b") is not None

        # LP weights are independent per model.
        a_weights = router._last_lp_weights["model-a"]
        b_weights = router._last_lp_weights["model-b"]
        # A's weights must only contain A's endpoints.
        for eid in a_weights:
            assert eid.startswith("model-a:")
        # B's weights must only contain B's endpoints.
        for eid in b_weights:
            assert eid.startswith("model-b:")


@pytest.mark.unit
class TestEnvelopeDonorBootstrap:
    """envelope_bootstrap_donor_models: donor rows seed the target's envelope."""

    def _router_with_donor_sibling(self):
        target_api = _make_adapter(
            provider_type="on_demand",
            prompt_price="1.0",
            completion_price="1.0",
            endpoint_id="target-model:api",
            model_id="target-model",
        )
        donor_api = _make_adapter(
            provider_type="on_demand",
            prompt_price="100.0",
            completion_price="100.0",
            endpoint_id="donor-model:api",
            model_id="donor-model",
        )
        fr = _FakeRouteTable()
        fr.add("target-model", [(target_api, 1.0)])
        fr.add("donor-model", [(donor_api, 1.0)])
        return RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

    def test_donor_rows_priced_with_target_routes_into_target_pool(self):
        router = self._router_with_donor_sibling()
        rows = [
            {
                "timestamp": time.time(),
                "model_id": "donor-model",
                "prompt_tokens": 1_000_000,
                "completion_tokens": 1_000_000,
            }
        ]

        counts = router.bootstrap_from_log_rows(
            rows,
            include_latency=False,
            include_envelope=True,
            envelope_model_overrides={"donor-model": "target-model"},
        )

        assert counts["envelope_samples"] == 1
        assert router.envelope.sample_count("target-model") == 1
        assert router.envelope.sample_count("donor-model") == 0
        snap = router.envelope.snapshot("target-model")
        assert snap is not None
        # Priced with target-model's $1/$1 per-M routes, not the donor's $100.
        assert snap.upper == pytest.approx(2.0)

    def test_without_overrides_donor_rows_stay_in_their_own_pool(self):
        router = self._router_with_donor_sibling()
        rows = [
            {
                "timestamp": time.time(),
                "model_id": "donor-model",
                "prompt_tokens": 1_000_000,
                "completion_tokens": 1_000_000,
            }
        ]

        router.bootstrap_from_log_rows(rows, include_latency=False, include_envelope=True)

        assert router.envelope.sample_count("target-model") == 0
        assert router.envelope.sample_count("donor-model") == 1


# ---------------------------------------------------------------------------
# S_C Concurrency provider tests (PR-6)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("chat", "stream"))
async def test_routewise_owned_execution_sets_context_and_ensures_before_error(operation):
    from serving.utils import context as req_ctx

    endpoint_id = f"test-model:context-{operation}"
    adapter = _make_adapter(
        provider="context-provider",
        endpoint_id=endpoint_id,
    )
    adapter.reports_leg_outcomes = False
    router = RouteWiseRouter(config=RouteWiseConfig(db_bootstrap_enabled=False))
    seen_contexts: list[dict[str, Any]] = []
    error = _StatusError(400, "bad request")

    async def _failed_chat(messages, **params):
        seen_contexts.append(dict(req_ctx.get()))
        raise error

    async def _failed_stream(messages, **params):
        seen_contexts.append(dict(req_ctx.get()))
        raise error
        yield  # pragma: no cover - marks this as an async generator

    adapter.chat_completion = _failed_chat
    adapter.stream_chat_completion = _failed_stream

    with req_ctx.push(user_id="context-test-user"):
        outer_context = dict(req_ctx.get())
        with pytest.raises(_StatusError, match="bad request"):
            if operation == "chat":
                await router._execute_adapter(_unreserved_decision(adapter), "test-model", [])
            else:
                async for _ in router._execute_stream_adapter(
                    _unreserved_decision(adapter), "test-model", []
                ):
                    pass
        assert req_ctx.get() == outer_context

    assert seen_contexts[-1]["model"] == "test-model"
    assert seen_contexts[-1]["provider"] == "context-provider"
    assert seen_contexts[-1]["user_id"] == "context-test-user"
    assert router.get_provider_status()[endpoint_id] == {
        "availability": 1.0,
        "circuit_state": "closed",
        "last_error_status": None,
        "consecutive_auth_rejections": 0,
    }


def _make_router_with_conc_and_api(
    config: RouteWiseConfig | None = None,
    prompt_price: str = "3.0",
    completion_price: str = "15.0",
    concurrency_limit: int = 4,
) -> tuple[RouteWiseRouter, MagicMock, MagicMock]:
    """Build a RouteWiseRouter with one S_C and one S_A adapter.

    Returns:
        (router, conc_adapter, api_adapter)
    """
    if config is None:
        config = RouteWiseConfig()
    conc_adapter = _make_adapter(
        provider_type="concurrency",
        prompt_price=prompt_price,
        completion_price=completion_price,
        endpoint_id="test-model:conc-provider",
        concurrency={"limit": concurrency_limit},
    )
    api_adapter = _make_adapter(
        provider_type="on_demand",
        prompt_price=prompt_price,
        completion_price=completion_price,
        endpoint_id="test-model:api-provider",
    )
    fr = _FakeRouteTable()
    fr.add("test-model", [(conc_adapter, 0.5), (api_adapter, 0.5)])
    router = RouteWiseRouter(route_table=fr, config=config)
    return router, conc_adapter, api_adapter


def _make_router_three_tier(
    config: RouteWiseConfig | None = None,
    concurrency_limit: int = 4,
    quota_limit: int = 5000,
) -> tuple[RouteWiseRouter, MagicMock, MagicMock, MagicMock]:
    """Build a RouteWiseRouter with S_C, S_Q, and S_A adapters.

    Returns:
        (router, conc_adapter, quota_adapter, api_adapter)
    """
    if config is None:
        config = RouteWiseConfig()
    conc_adapter = _make_adapter(
        provider_type="concurrency",
        prompt_price="3.0",
        completion_price="15.0",
        endpoint_id="test-model:conc-provider",
        concurrency={"limit": concurrency_limit},
    )
    quota_adapter = _make_adapter(
        provider_type="quota",
        prompt_price="3.0",
        completion_price="15.0",
        endpoint_id="test-model:quota-provider",
        quota={"limit": quota_limit},
    )
    api_adapter = _make_adapter(
        provider_type="on_demand",
        prompt_price="3.0",
        completion_price="15.0",
        endpoint_id="test-model:api-provider",
    )
    fr = _FakeRouteTable()
    fr.add(
        "test-model",
        [
            (conc_adapter, 0.3),
            (quota_adapter, 0.3),
            (api_adapter, 0.4),
        ],
    )
    router = RouteWiseRouter(route_table=fr, config=config)
    _seed_quota_snapshots(router)
    return router, conc_adapter, quota_adapter, api_adapter


@pytest.mark.unit
class TestRouteWiseSCDecision:
    """Decision tests for S_C concurrency providers."""

    def test_sc_routes_to_concurrency_when_available(self):
        """S_C selected when slots are available."""
        router, conc_adapter, _api_adapter = _make_router_with_conc_and_api()

        # Warm predictor so v_t is meaningful.
        for _ in range(25):
            router.predictor.update("test-model", 500)

        selected = router._select_decision("test-model", {"prompt_tokens": 1000}).adapter
        assert selected is conc_adapter
        assert _conc_pool(router).active == 1

    def test_sc_routes_to_api_when_full(self):
        """Falls to S_A when S_C slots are exhausted."""
        router, _conc_adapter, api_adapter = _make_router_with_conc_and_api(concurrency_limit=1)

        for _ in range(25):
            router.predictor.update("test-model", 500)

        # Fill the single slot.
        _conc_pool(router).try_acquire()

        selected = router._select_decision("test-model", {"prompt_tokens": 1000}).adapter
        assert selected is api_adapter

    def test_sc_preferred_over_sq_when_both_available(self):
        """S_C beats S_Q because gain_C = v_t > v_t - theta_Q = gain_Q."""
        router, conc_adapter, _quota_adapter, _api_adapter = _make_router_three_tier()
        _warm_envelope(router, lower=0.001, upper=0.500)

        for _ in range(25):
            router.predictor.update("test-model", 500)

        selected = router._select_decision("test-model", {"prompt_tokens": 1000}).adapter
        assert selected is conc_adapter

    def test_sc_pool_built_from_route_policy(self):
        """Concurrency pools come from the route-level policy; no model flag."""
        router, _conc_adapter, _api_adapter = _make_router_with_conc_and_api(concurrency_limit=2)
        assert len(router.concurrency_pools) == 1
        assert _conc_pool(router).limit == 2

    def test_sc_pool_limit_rebuild_preserves_active_slots(self):
        """Changing a route limit must not reset in-flight reservations."""
        router, conc_adapter, _api_adapter = _make_router_with_conc_and_api(concurrency_limit=2)
        pool = _conc_pool(router)
        assert pool.try_acquire() is True
        assert pool.try_acquire() is True

        conc_adapter.config.concurrency = {"limit": 3}
        router._rebuild_from_route_table()

        updated_pool = _conc_pool(router)
        assert updated_pool is pool
        assert updated_pool.limit == 3
        assert updated_pool.active == 2
        assert updated_pool.available == 1
        assert updated_pool.try_acquire() is True

        conc_adapter.config.concurrency = {"limit": 2}
        router._rebuild_from_route_table()

        assert _conc_pool(router) is pool
        assert pool.limit == 2
        assert pool.active == 3
        assert pool.available == 0
        assert pool.try_acquire() is False

    def test_two_concurrency_pools_do_not_share_slots(self):
        """Each pool has its own slots; saturating one leaves the other free."""
        conc_a = _make_adapter(
            provider_type="concurrency",
            endpoint_id="test-model:conc-a",
            concurrency={"limit": 1},
        )
        conc_b = _make_adapter(
            provider_type="concurrency",
            endpoint_id="test-model:conc-b",
            concurrency={"limit": 1},
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(conc_a, 0.5), (conc_b, 0.5)])
        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

        assert len(router.concurrency_pools) == 2
        pool_a = router.concurrency_pools["test-model:test-model:conc-a"]
        pool_b = router.concurrency_pools["test-model:test-model:conc-b"]
        assert pool_a.try_acquire() is True
        assert pool_a.available == 0
        assert pool_b.available == 1

        for _ in range(25):
            router.predictor.update("test-model", 500)
        selected = router._select_decision("test-model", {"prompt_tokens": 1000}).adapter
        assert selected is conc_b

    def test_conflicting_shared_pool_policies_rejected(self):
        """Two routes sharing a pool id must declare identical policies."""
        conc_a = _make_adapter(
            provider_type="concurrency",
            endpoint_id="test-model:conc-a",
            concurrency={"limit": 1},
            concurrency_pool="shared-subscription",
        )
        conc_b = _make_adapter(
            provider_type="concurrency",
            endpoint_id="test-model:conc-b",
            concurrency={"limit": 2},
            concurrency_pool="shared-subscription",
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(conc_a, 0.5), (conc_b, 0.5)])
        with pytest.raises(ValueError, match="conflicting limits"):
            RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

    def test_shared_pool_routes_share_slots(self):
        """Routes declaring the same pool id draw from one slot budget."""
        conc_a = _make_adapter(
            provider_type="concurrency",
            endpoint_id="test-model:conc-a",
            concurrency={"limit": 1},
            concurrency_pool="shared-subscription",
        )
        conc_b = _make_adapter(
            provider_type="concurrency",
            endpoint_id="test-model:conc-b",
            concurrency={"limit": 1},
            concurrency_pool="shared-subscription",
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(conc_a, 0.5), (conc_b, 0.5)])
        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

        assert len(router.concurrency_pools) == 1
        assert router.concurrency_pools["shared-subscription"].try_acquire() is True

        for _ in range(25):
            router.predictor.update("test-model", 500)
        decision = router._select_decision("test-model", {"prompt_tokens": 1000})
        assert decision is None

    def test_sc_full_sq_available_routes_to_sq(self):
        """When S_C is full, falls to S_Q if theta_Q condition met."""
        router, _conc_adapter, quota_adapter, _api_adapter = _make_router_three_tier(
            concurrency_limit=1
        )
        _warm_envelope(router, lower=0.0000001, upper=0.001)

        for _ in range(25):
            router.predictor.update("test-model", 500)

        # Fill S_C.
        _conc_pool(router).try_acquire()

        selected = router._select_decision("test-model", {"prompt_tokens": 1000}).adapter
        assert selected is quota_adapter

    def test_sc_full_sq_exhausted_routes_to_api(self):
        """When both S_C and S_Q exhausted, routes to S_A."""
        router, _conc_adapter, _quota_adapter, api_adapter = _make_router_three_tier(
            concurrency_limit=1, quota_limit=100
        )
        _warm_envelope(router, lower=0.0000001, upper=0.001)

        for _ in range(25):
            router.predictor.update("test-model", 500)

        # Fill S_C.
        _conc_pool(router).try_acquire()
        # Exhaust S_Q.
        for _ in range(100):
            _quota_pool(router).consume()

        selected = router._select_decision("test-model", {"prompt_tokens": 1000}).adapter
        assert selected is api_adapter

    def test_three_tier_priority_cascade(self):
        """Full cascade: S_C -> S_Q -> S_A as resources deplete."""
        router, conc_adapter, quota_adapter, api_adapter = _make_router_three_tier(
            concurrency_limit=1, quota_limit=1
        )
        _warm_envelope(router, lower=0.0000001, upper=0.001)

        for _ in range(25):
            router.predictor.update("test-model", 500)

        # First request: S_C (slot available).
        sel1 = router._select_decision("test-model", {"prompt_tokens": 1000}).adapter
        assert sel1 is conc_adapter

        # S_C now full (limit=1). Second request: S_Q.
        sel2 = router._select_decision("test-model", {"prompt_tokens": 1000}).adapter
        assert sel2 is quota_adapter

        # S_Q now exhausted (quota=1). Third request: S_A.
        sel3 = router._select_decision("test-model", {"prompt_tokens": 1000}).adapter
        assert sel3 is api_adapter

    def test_commit_retry_keeps_resolving_until_candidate_success(self, monkeypatch):
        """Commit races remove the failed candidate and continue until success."""
        router, _conc_adapter, _quota_adapter, api_adapter = _make_router_three_tier()
        _warm_envelope(router, lower=0.001, upper=0.500)

        for _ in range(25):
            router.predictor.update("test-model", 500)

        sampled_attempts: list[list[str]] = []
        commit_attempts: list[str] = []

        def sample_first_candidate(candidates, _solution):
            sampled_attempts.append([candidate.endpoint_id for candidate in candidates])
            return candidates[0]

        def fail_first_two_commits(candidate):
            commit_attempts.append(candidate.endpoint_id)
            if len(commit_attempts) >= 3:
                return ProviderReservation()
            return None

        monkeypatch.setattr(router, "_sample_solution", sample_first_candidate)
        monkeypatch.setattr(router, "_reserve_candidate", fail_first_two_commits)

        decision = router._select_decision(
            "test-model",
            {"prompt_tokens": 1000, "request_id": "req-commit-retry"},
        )

        assert decision.adapter is api_adapter
        assert sampled_attempts == [
            [
                "test-model:conc-provider",
                "test-model:quota-provider",
                "test-model:api-provider",
            ],
            ["test-model:quota-provider", "test-model:api-provider"],
            ["test-model:api-provider"],
        ]
        assert commit_attempts == [
            "test-model:conc-provider",
            "test-model:quota-provider",
            "test-model:api-provider",
        ]
        meta = decision.metadata
        assert meta["selected_endpoint"] == "test-model:api-provider"
        assert meta["selected_provider_type"] == "on_demand"


@pytest.mark.unit
class TestRouteWiseSCLifecycle:
    """Async slot lifecycle tests for S_C concurrency providers."""

    @pytest.mark.asyncio
    async def test_slot_acquired_and_released_on_success(self):
        """Slot released after successful execution."""
        router, conc_adapter, _api_adapter = _make_router_with_conc_and_api()

        for _ in range(25):
            router.predictor.update("test-model", 500)

        # Select -> acquires slot.
        decision = router._select_decision("test-model", {"prompt_tokens": 1000})
        assert decision.adapter is conc_adapter
        assert _conc_pool(router).active == 1

        conc_adapter.reports_leg_outcomes = False
        conc_adapter.chat_completion = AsyncMock(
            return_value={"choices": [{"message": {"content": "ok"}}]}
        )
        await router._execute_adapter(
            decision,
            "test-model",
            [{"role": "user", "content": "hi"}],
        )
        assert _conc_pool(router).active == 0

    @pytest.mark.asyncio
    async def test_slot_released_on_provider_error(self):
        """Slot released even when adapter raises an exception."""
        router, conc_adapter, _api_adapter = _make_router_with_conc_and_api()

        for _ in range(25):
            router.predictor.update("test-model", 500)

        decision = router._select_decision("test-model", {"prompt_tokens": 1000})
        assert decision.adapter is conc_adapter
        assert _conc_pool(router).active == 1

        conc_adapter.reports_leg_outcomes = False
        conc_adapter.chat_completion = AsyncMock(side_effect=RuntimeError("provider error"))
        with pytest.raises(RuntimeError, match="provider error"):
            await router._execute_adapter(
                decision,
                "test-model",
                [{"role": "user", "content": "hi"}],
            )
        assert _conc_pool(router).active == 0

    @pytest.mark.parametrize("operation", ("chat", "stream"))
    @pytest.mark.asyncio
    async def test_slot_released_when_endpoint_resolution_raises(self, operation):
        """Capacity release still runs when endpoint metadata is malformed."""
        router, conc_adapter, _api_adapter = _make_router_with_conc_and_api()

        for _ in range(25):
            router.predictor.update("test-model", 500)

        decision = router._select_decision("test-model", {"prompt_tokens": 1000})
        assert decision.adapter is conc_adapter
        assert _conc_pool(router).active == 1

        with (
            patch(
                "routing.routewise.router.endpoint_id_for_adapter",
                side_effect=RuntimeError("invalid endpoint metadata"),
            ),
            pytest.raises(RuntimeError, match="invalid endpoint metadata"),
        ):
            if operation == "chat":
                await router._execute_adapter(decision, "test-model", [])
            else:
                async for _ in router._execute_stream_adapter(decision, "test-model", []):
                    pass

        assert _conc_pool(router).active == 0

    @pytest.mark.asyncio
    async def test_slot_released_on_cancel(self):
        """Slot released on asyncio.CancelledError."""
        router, conc_adapter, _api_adapter = _make_router_with_conc_and_api()

        for _ in range(25):
            router.predictor.update("test-model", 500)

        decision = router._select_decision("test-model", {"prompt_tokens": 1000})
        assert decision.adapter is conc_adapter
        assert _conc_pool(router).active == 1

        conc_adapter.reports_leg_outcomes = False
        conc_adapter.chat_completion = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await router._execute_adapter(
                decision,
                "test-model",
                [{"role": "user", "content": "hi"}],
            )
        assert _conc_pool(router).active == 0

    @pytest.mark.asyncio
    async def test_stream_slot_released_on_completion(self):
        """Streaming: slot released after generator exhaustion."""
        router, conc_adapter, _api_adapter = _make_router_with_conc_and_api()

        for _ in range(25):
            router.predictor.update("test-model", 500)

        decision = router._select_decision("test-model", {"prompt_tokens": 1000})
        assert decision.adapter is conc_adapter
        assert _conc_pool(router).active == 1

        async def _fake_stream(*args, **kwargs):
            yield {"choices": [{"delta": {"content": "hello"}}]}
            yield {"choices": [{"delta": {"content": " world"}}]}

        conc_adapter.reports_leg_outcomes = False
        conc_adapter.stream_chat_completion = _fake_stream
        chunks = []
        async for chunk in router._execute_stream_adapter(
            decision,
            "test-model",
            [{"role": "user", "content": "hi"}],
        ):
            chunks.append(chunk)
        assert len(chunks) == 2
        assert _conc_pool(router).active == 0

    @pytest.mark.asyncio
    async def test_no_slot_for_api_request(self):
        """S_A execution does not touch conc_mgr."""
        router, _conc_adapter, api_adapter = _make_router_with_conc_and_api(concurrency_limit=1)

        for _ in range(25):
            router.predictor.update("test-model", 500)

        # Fill S_C so next request goes to S_A.
        _conc_pool(router).try_acquire()
        decision = router._select_decision("test-model", {"prompt_tokens": 1000})
        assert decision.adapter is api_adapter
        assert _conc_pool(router).active == 1  # From manual acquire.

        api_adapter.reports_leg_outcomes = False
        api_adapter.chat_completion = AsyncMock(
            return_value={"choices": [{"message": {"content": "ok"}}]}
        )
        await router._execute_adapter(
            decision,
            "test-model",
            [{"role": "user", "content": "hi"}],
        )
        # conc_mgr unchanged -- S_A doesn't release.
        assert _conc_pool(router).active == 1

    @pytest.mark.asyncio
    async def test_no_slot_for_quota_request(self):
        """S_Q execution does not touch conc_mgr."""
        # Need a router with S_Q + S_C + S_A.
        router, _conc_adapter, quota_adapter, _api_adapter = _make_router_three_tier()
        _warm_envelope(router, lower=0.0000001, upper=0.001)

        for _ in range(25):
            router.predictor.update("test-model", 500)

        # Fill S_C so next request falls to S_Q.
        for _ in range(4):
            _conc_pool(router).try_acquire()
        assert _conc_pool(router).active == 4

        decision = router._select_decision("test-model", {"prompt_tokens": 1000})
        assert decision.adapter is quota_adapter

        quota_adapter.reports_leg_outcomes = False
        quota_adapter.chat_completion = AsyncMock(
            return_value={"choices": [{"message": {"content": "ok"}}]}
        )
        await router._execute_adapter(
            decision,
            "test-model",
            [{"role": "user", "content": "hi"}],
        )
        # conc_mgr unchanged -- S_Q doesn't release.
        assert _conc_pool(router).active == 4

    def test_rebuild_releases_the_pool_captured_at_acquire_time(self):
        """A decision releases its old pool even after route state is rebuilt."""
        router, conc_adapter, _api_adapter = _make_router_with_conc_and_api(concurrency_limit=2)
        decision = router._select_decision("test-model", {"prompt_tokens": 1000})
        assert decision.adapter is conc_adapter
        old_pool = _conc_pool(router)
        assert old_pool.active == 1

        conc_adapter.config.concurrency_pool = "replacement-pool"
        router._rebuild_from_route_table()
        replacement_pool = router.concurrency_pools["replacement-pool"]
        assert replacement_pool is not old_pool
        assert replacement_pool.try_acquire() is True
        assert replacement_pool.active == 1

        decision.release()
        decision.release()

        assert old_pool.active == 0
        assert replacement_pool.active == 1
        replacement_pool.release()

    @pytest.mark.parametrize("failure_stage", ("metadata", "hedge_plan", "hedged_adapter"))
    def test_selection_construction_failure_releases_acquired_slot(
        self,
        monkeypatch,
        failure_stage,
    ):
        """Failures after acquire cannot strand a concurrency reservation."""
        router, conc_adapter, _api_adapter = _make_router_with_conc_and_api()

        def _choose_concurrency(candidates, _solution):
            return next(candidate for candidate in candidates if candidate.adapter is conc_adapter)

        def _raise_construction_error(*args, **kwargs):
            raise RuntimeError(f"{failure_stage} construction failed")

        monkeypatch.setattr(router, "_sample_solution", _choose_concurrency)
        if failure_stage == "metadata":
            monkeypatch.setattr(router, "_decision_metadata", _raise_construction_error)
        elif failure_stage == "hedge_plan":
            monkeypatch.setattr(router, "_select_hedge_plan", _raise_construction_error)
        else:
            hedge_plan = MagicMock(checkpoints_sec=(0.01,))
            monkeypatch.setattr(router, "_select_hedge_plan", lambda **_kwargs: hedge_plan)
            monkeypatch.setattr(
                "routing.routewise.router.HedgedAdapter",
                MagicMock(side_effect=_raise_construction_error),
            )

        with pytest.raises(RuntimeError, match=f"{failure_stage} construction failed"):
            router._select_decision("test-model", {"prompt_tokens": 1000})

        assert _conc_pool(router).active == 0


# ---------------------------------------------------------------------------
# No-S_A edge case tests (codex review fix)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRouteWiseNoApiBaseline:
    """Ensure no accounting bypass when S_A baseline is absent."""

    def test_sc_only_full_returns_none(self):
        """S_C-only config: when slots are full, returns None (not S_C)."""
        conc = _make_adapter(
            provider_type="concurrency",
            endpoint_id="test-model:conc",
            concurrency={"limit": 1},
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(conc, 1.0)])

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

        # Fill the single slot externally.
        _conc_pool(router).try_acquire()
        assert _conc_pool(router).active == 1

        decision = router._select_decision("test-model", {"prompt_tokens": 100})
        assert decision is None
        # Active must not change -- no spurious acquire or release.
        assert _conc_pool(router).active == 1

    def test_sq_only_exhausted_returns_none(self):
        """S_Q-only config: when quota exhausted, returns None (not S_Q)."""
        quota = _make_adapter(
            provider_type="quota",
            endpoint_id="test-model:quota",
            quota={"limit": 5},
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(quota, 1.0)])

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        _seed_quota_snapshots(router)

        # Exhaust quota.
        for _ in range(5):
            _quota_pool(router).consume()
        assert _quota_pool(router).remaining == 0

        decision = router._select_decision("test-model", {"prompt_tokens": 100})
        assert decision is None
        # Quota must not change.
        assert _quota_pool(router).remaining == 0

    def test_sc_sq_no_api_all_depleted_returns_none(self):
        """S_C + S_Q but no S_A: returns None when both depleted."""
        conc = _make_adapter(
            provider_type="concurrency",
            endpoint_id="test-model:conc",
            concurrency={"limit": 1},
        )
        quota = _make_adapter(
            provider_type="quota",
            endpoint_id="test-model:quota",
            quota={"limit": 3},
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(conc, 0.5), (quota, 0.5)])

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        _seed_quota_snapshots(router)

        # Fill S_C.
        _conc_pool(router).try_acquire()
        # Exhaust S_Q.
        for _ in range(3):
            _quota_pool(router).consume()

        decision = router._select_decision("test-model", {"prompt_tokens": 100})
        assert decision is None
        assert _conc_pool(router).active == 1
        assert _quota_pool(router).remaining == 0

    def test_sc_only_available_still_routes(self):
        """S_C-only config: routes to S_C when slots available (v_t=inf ok)."""
        conc = _make_adapter(
            provider_type="concurrency",
            endpoint_id="test-model:conc",
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(conc, 1.0)])

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

        selected = router._select_decision("test-model", {"prompt_tokens": 100}).adapter
        assert selected is conc
        assert _conc_pool(router).active == 1

    @pytest.mark.asyncio
    async def test_stream_close_after_routing_chunk_releases_sc_slot(self):
        """Closing before provider streaming starts must not leak the S_C slot."""
        conc = _make_adapter(
            provider_type="concurrency",
            endpoint_id="test-model:conc",
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(conc, 1.0)])

        stream_entered = False

        async def _stream(*args, **kwargs):
            nonlocal stream_entered
            stream_entered = True
            yield 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'

        conc.stream_chat_completion = _stream

        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

        stream = router.stream_chat_completion(
            "test-model",
            [{"role": "user", "content": "hi"}],
            request_id="req-close-before-provider",
        )
        first = await stream.__anext__()

        assert isinstance(first, str)
        assert '"_routing"' in first
        assert stream_entered is False
        assert _conc_pool(router).active == 1

        await stream.aclose()

        assert _conc_pool(router).active == 0

    @pytest.mark.asyncio
    async def test_stream_close_after_provider_payload_releases_only_its_slot(self):
        """Closing mid-stream finalizes the inner stream and releases exactly once."""
        conc = _make_adapter(
            provider_type="concurrency",
            endpoint_id="test-model:conc",
            concurrency={"limit": 2},
        )
        conc.reports_leg_outcomes = False
        fr = _FakeRouteTable()
        fr.add("test-model", [(conc, 1.0)])
        inner_finalized = asyncio.Event()

        async def _stream(*args, **kwargs):
            try:
                yield 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                await asyncio.Event().wait()
            finally:
                inner_finalized.set()

        conc.stream_chat_completion = _stream
        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        router.pending_prefix_cache.touch = MagicMock(wraps=router.pending_prefix_cache.touch)
        pool = _conc_pool(router)
        assert pool.try_acquire() is True  # Unrelated in-flight request.

        stream = router.stream_chat_completion(
            "test-model",
            [{"role": "user", "content": "hi"}],
            request_id="req-close-after-payload",
        )
        routing = await stream.__anext__()
        payload = await stream.__anext__()
        assert '"_routing"' in routing
        assert '"content":"hi"' in payload
        assert pool.active == 2
        router.pending_prefix_cache.touch.assert_called_once_with("req-close-after-payload")

        await stream.aclose()
        await asyncio.wait_for(inner_finalized.wait(), timeout=1.0)
        await stream.aclose()

        assert pool.active == 1
        pool.release()
        assert pool.active == 0

    def test_validation_warns_no_on_demand_baseline(self):
        """Construction-time warning when model has no P_O adapter."""
        conc = _make_adapter(
            provider_type="concurrency",
            endpoint_id="test-model:conc",
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(conc, 1.0)])

        with patch("routing.routewise.router.logger") as mock_logger:
            RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
            mock_logger.warning.assert_called()
            warning_msg = mock_logger.warning.call_args[0][0]
            assert "no P_O" in warning_msg


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
        config = RouteWiseConfig()
    conc_adapter = _make_adapter(
        provider_type="concurrency",
        prompt_price="3.0",
        completion_price="15.0",
        endpoint_id="test-model:conc-provider",
    )
    quota_adapter = _make_adapter(
        provider_type="quota",
        prompt_price="3.0",
        completion_price="15.0",
        endpoint_id="test-model:quota-provider",
    )
    api_adapter = _make_adapter(
        provider_type="on_demand",
        prompt_price="3.0",
        completion_price="15.0",
        endpoint_id="test-model:api-provider",
    )
    fr = _FakeRouteTable()
    fr.add(
        "test-model",
        [(conc_adapter, 0.3), (quota_adapter, 0.3), (api_adapter, 0.4)],
    )
    router = RouteWiseRouter(route_table=fr, config=config)
    return router, conc_adapter, quota_adapter, api_adapter


@pytest.mark.unit
class TestRouteWiseDecisionMetadata:
    """Verify request-local decision metadata is merged into responses."""

    def test_sc_decision_stores_metadata(self):
        """S_C selection stores metadata with selected_provider_type='concurrency'."""
        router, conc, _quota, _api = _make_router_with_all_tiers()
        request_id = "req-test-sc"
        context = {"request_id": request_id}

        decision = router._select_decision("test-model", context)
        assert decision.adapter is conc
        assert decision.trace.request_id == request_id

        meta = decision.metadata
        assert meta["selected_provider_type"] == "concurrency"
        assert meta["sc_committed"] is True
        assert meta["quota_committed"] == 0.0
        assert meta["hedged"] is False
        assert meta["backup_won"] is False

    def test_decision_metadata_carries_canonical_h6_fields(self):
        """Decision metadata exposes H6 canonical fields aligned with SIM/REAL.

        These land in ``api_logs.metadata["routewise"]`` so cross-source parity
        reads one field-name contract. The default config does not dispatch
        hedges, so hedge fields are disabled/None.
        """
        router, _conc, _quota, _api = _make_router_with_all_tiers()
        request_id = "req-test-canonical"
        context = {"request_id": request_id}

        decision = router._select_decision("test-model", context)
        meta = decision.metadata

        assert meta["policy"] == "routewise"
        # Canonical aliases agree with the prod-native fields.
        assert meta["primary_provider_type"] == meta["selected_provider_type"]
        assert meta["primary_provider"] == meta["selected_endpoint"]
        assert meta["lp_budget_usd"] == meta["budget_usd"]
        # Hedging is not dispatched in production.
        assert meta["hedge_triggered"] is False
        assert meta["hedge_algorithm"] == "disabled"
        assert meta["hedge_schedule"] is None
        assert meta["backup_provider"] is None
        assert meta["backup_provider_type"] is None
        assert meta["hedge_winner"] is None
        assert meta["primary_routing_estimated_cost_usd"] == pytest.approx(
            meta["selected_effective_cost_usd"]
            if meta["selected_provider_type"] == "on_demand"
            else meta["candidate_request_costs_usd"][meta["selected_endpoint"]]
        )
        assert meta["backup_routing_estimated_cost_usd"] is None
        assert meta["routing_estimated_cost_usd"] == pytest.approx(
            meta["primary_routing_estimated_cost_usd"]
        )
        # lp_weights / lp_status already use canonical names.
        assert "lp_weights" in meta
        assert "lp_status" in meta

    def test_sq_decision_stores_metadata(self):
        """S_Q selection stores metadata with selected_provider_type='quota'."""
        router, quota, _api = _make_router_with_quota_and_api()
        _warm_envelope(router, lower=0.0000001, upper=0.001)
        request_id = "req-test-sq"
        context = {"request_id": request_id}

        decision = router._select_decision("test-model", context)
        assert decision.adapter is quota
        assert decision.trace.request_id == request_id

        meta = decision.metadata
        assert meta["selected_provider_type"] == "quota"
        assert (
            meta["quota_committed"] == 0.0
        )  # commitment signal via selected_provider_type, not v_t
        assert meta["v_t"] > 0  # value estimation lives in its own field
        assert meta["sc_committed"] is False
        assert meta["hedged"] is False

    def test_sa_decision_stores_metadata(self):
        """S_A selection stores metadata with selected_provider_type='on_demand'."""
        # Make quota too expensive by warming the envelope above any API cost.
        router, _quota, api = _make_router_with_quota_and_api(quota_limit=1000)
        _warm_envelope(router, lower=1000.0, upper=10000.0)
        request_id = "req-test-sa"
        context = {"request_id": request_id}

        decision = router._select_decision("test-model", context)
        assert decision.adapter is api
        assert decision.trace.request_id == request_id

        meta = decision.metadata
        assert meta["selected_provider_type"] == "on_demand"
        assert meta["quota_committed"] == 0.0
        assert meta["sc_committed"] is False

    def test_no_request_id_still_works(self):
        """Selection without request_id still returns request-local metadata."""
        router, _quota, _api = _make_router_with_quota_and_api()
        decision = router._select_decision("test-model", {})
        assert decision is not None
        assert decision.trace.request_id is None
        assert "selected_provider_type" in decision.metadata

    def test_same_request_id_decisions_remain_isolated(self):
        """Repeated external ids do not alias request-local decision state."""
        concurrency = _make_adapter(
            provider_type="concurrency",
            concurrency={"limit": 2},
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(concurrency, 1.0)])
        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

        first = router._select_decision("test-model", {"request_id": "reused-id"})
        second = router._select_decision("test-model", {"request_id": "reused-id"})
        assert first is not None
        assert second is not None
        pool = _conc_pool(router)
        assert pool.active == 2
        first.metadata["backup_won"] = True
        first.trace.record_failed_attempt({"endpoint_id": "first-only"})

        assert first is not second
        assert first.metadata is not second.metadata
        assert first.trace is not second.trace
        assert second.metadata["backup_won"] is False
        assert second.trace.failed_attempts == []
        assert not hasattr(router, "_pending_decisions")
        assert not hasattr(router, "_primary_reservations")

        first.release()
        assert pool.active == 1
        second.release()
        assert pool.active == 0

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
        assert rw["selected_provider_type"] in ("quota", "on_demand")
        assert "v_t" in rw
        assert not hasattr(router, "_pending_decisions")

    @pytest.mark.asyncio
    async def test_chat_completion_re_solves_routewise_after_provider_failure(self):
        """A failed RouteWise selection retries by re-solving, not generic on-demand fallback."""
        quota = _make_adapter(
            provider_type="quota",
            endpoint_id="test-model:quota-provider",
            quota={"limit": 10_000},
        )
        conc = _make_adapter(
            provider_type="concurrency",
            endpoint_id="test-model:conc-provider",
            concurrency={"limit": 1},
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(quota, 0.5), (conc, 0.5)])
        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        _seed_quota_snapshots(router)
        _warm_envelope(router, lower=0.0000001, upper=0.001)

        def _choose(candidates, _solution):
            by_id = {candidate.endpoint_id: candidate for candidate in candidates}
            return by_id.get("test-model:quota-provider") or by_id["test-model:conc-provider"]

        router._sample_solution = _choose  # type: ignore[method-assign]
        quota.chat_completion = AsyncMock(side_effect=_StatusError(429, "quota 429"))
        conc.chat_completion = AsyncMock(
            return_value={"choices": [{"message": {"content": "fallback ok"}}]}
        )

        resp = await router.chat_completion(
            "test-model",
            [{"role": "user", "content": "hi"}],
            request_id="req-policy-fallback",
        )

        assert resp["_routing"]["endpoint_id"] == "test-model:conc-provider"
        assert resp["_routing"]["fallback"] is True
        assert resp["_routing"]["fallback_policy"] == "routewise_resolve"
        assert quota.chat_completion.await_count == 1
        assert conc.chat_completion.await_count == 1

        rw = resp["_routing"]["routewise"]
        assert rw["initial_selected_endpoint"] == "test-model:quota-provider"
        assert rw["selected_endpoint"] == "test-model:conc-provider"
        assert rw["final_endpoint"] == "test-model:conc-provider"
        assert rw["fallback_policy"] == "routewise_resolve"
        assert rw["fallback_attempts"] == 1
        assert rw["failed_attempts"][0]["endpoint_id"] == "test-model:quota-provider"
        assert _quota_pool(router).remaining == 9999
        assert _conc_pool(router).active == 0

    @pytest.mark.asyncio
    async def test_chat_retry_exhaustion_keeps_last_decision_metadata(self):
        """A retryable single-provider failure retains its terminal trace."""
        primary = _make_adapter(
            provider="primary",
            provider_type="on_demand",
            endpoint_id="test-model:primary",
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(primary, 1.0)])
        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        primary.chat_completion = AsyncMock(side_effect=_StatusError(503, "unavailable"))

        with pytest.raises(_StatusError, match="unavailable") as exc_info:
            await router.chat_completion(
                "test-model",
                [{"role": "user", "content": "hi"}],
                request_id="req-exhausted",
            )

        routing = exc_info.value._routing
        routewise = routing["routewise"]
        assert routewise["selected_endpoint"] == "test-model:primary"
        assert routewise["fallback_policy"] == "routewise_resolve"
        assert routewise["fallback_attempts"] == 1
        assert routewise["fallback_excluded_endpoints"] == ["test-model:primary"]

    @pytest.mark.asyncio
    async def test_chat_completion_does_not_resolve_after_nonretryable_error(self):
        """Non-transient provider errors surface directly but keep S_Q attempts charged."""
        quota = _make_adapter(
            provider_type="quota",
            endpoint_id="test-model:quota-provider",
            quota={"limit": 10_000},
        )
        conc = _make_adapter(
            provider_type="concurrency",
            endpoint_id="test-model:conc-provider",
            concurrency={"limit": 1},
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(quota, 0.5), (conc, 0.5)])
        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        _seed_quota_snapshots(router)
        _warm_envelope(router, lower=0.0000001, upper=0.001)

        def _choose(candidates, _solution):
            by_id = {candidate.endpoint_id: candidate for candidate in candidates}
            return by_id.get("test-model:quota-provider") or by_id["test-model:conc-provider"]

        router._sample_solution = _choose  # type: ignore[method-assign]
        quota.chat_completion = AsyncMock(side_effect=_StatusError(400, "bad request"))
        conc.chat_completion = AsyncMock(
            return_value={"choices": [{"message": {"content": "should not run"}}]}
        )

        with pytest.raises(_StatusError, match="bad request") as exc_info:
            await router.chat_completion(
                "test-model",
                [{"role": "user", "content": "hi"}],
                request_id="req-nonretryable",
            )

        assert quota.chat_completion.await_count == 1
        assert conc.chat_completion.await_count == 0
        assert _quota_pool(router).remaining == 9999

        routing = getattr(exc_info.value, "_routing", None)
        assert routing is not None
        assert "routewise" in routing
        assert routing["routewise"]["fallback_attempts"] == 0
        assert routing["routewise"]["fallback_policy"] is None

    @pytest.mark.asyncio
    async def test_strict_fallback_mode_does_not_resolve_after_retryable_error(self):
        """fallback_mode='strict' surfaces even a retryable failure without re-solving."""
        quota = _make_adapter(
            provider_type="quota",
            endpoint_id="test-model:quota-provider",
            quota={"limit": 10_000},
        )
        conc = _make_adapter(
            provider_type="concurrency",
            endpoint_id="test-model:conc-provider",
            concurrency={"limit": 1},
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(quota, 0.5), (conc, 0.5)])
        router = RouteWiseRouter(
            route_table=fr,
            config=RouteWiseConfig(fallback_mode="strict"),
        )
        _seed_quota_snapshots(router)
        _warm_envelope(router, lower=0.0000001, upper=0.001)

        def _choose(candidates, _solution):
            by_id = {candidate.endpoint_id: candidate for candidate in candidates}
            return by_id.get("test-model:quota-provider") or by_id["test-model:conc-provider"]

        router._sample_solution = _choose  # type: ignore[method-assign]
        # A 429 is retryable; in policy mode it would re-solve onto conc, but
        # strict mode must surface the failure and never touch the backup.
        quota.chat_completion = AsyncMock(side_effect=_StatusError(429, "quota 429"))
        conc.chat_completion = AsyncMock(
            return_value={"choices": [{"message": {"content": "should not run"}}]}
        )

        with pytest.raises(_StatusError, match="quota 429") as exc_info:
            await router.chat_completion(
                "test-model",
                [{"role": "user", "content": "hi"}],
                request_id="req-strict",
            )

        assert quota.chat_completion.await_count == 1
        assert conc.chat_completion.await_count == 0
        assert _quota_pool(router).remaining == 9999

        routing = getattr(exc_info.value, "_routing", None)
        assert routing is not None
        assert routing["routewise"]["fallback_attempts"] == 0
        assert routing["routewise"]["fallback_policy"] is None

    def test_invalid_fallback_mode_rejected(self):
        """RouteWiseConfig rejects an unknown fallback_mode at construction."""
        with pytest.raises(ValueError, match="fallback_mode"):
            RouteWiseConfig(fallback_mode="bogus")  # type: ignore[arg-type]

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
                assert parsed["_routing"]["routewise"]["selected_provider_type"] in (
                    "quota",
                    "on_demand",
                )
                routewise_found = True
                break
        assert routewise_found, "Routewise metadata chunk not found in stream"

        # [DONE] should be last
        assert chunks[-1].strip() == "data: [DONE]"

        assert not hasattr(router, "_pending_decisions")

    @pytest.mark.asyncio
    async def test_stream_does_not_fallback_after_provider_chunk(self):
        """Once a provider chunk is emitted, fallback would corrupt the SSE stream."""
        primary = _make_adapter(
            provider="primary",
            provider_type="on_demand",
            endpoint_id="test-model:primary",
        )
        backup = _make_adapter(
            provider="backup",
            provider_type="on_demand",
            endpoint_id="test-model:backup",
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(primary, 0.5), (backup, 0.5)])
        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

        async def _primary_stream(*args, **kwargs):
            yield 'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise RuntimeError("primary stream failed mid-flight")

        async def _backup_stream(*args, **kwargs):
            yield 'data: {"choices":[{"delta":{"content":"backup"}}]}\n\n'
            yield "data: [DONE]\n\n"

        primary.stream_chat_completion = _primary_stream
        backup.stream_chat_completion = _backup_stream

        def _choose_primary(candidates, _solution):
            return next(candidate for candidate in candidates if candidate.adapter is primary)

        router._sample_solution = _choose_primary  # type: ignore[method-assign]

        chunks: list[str] = []
        with pytest.raises(RuntimeError, match="primary stream failed mid-flight"):
            async for chunk in router.stream_chat_completion(
                "test-model",
                [{"role": "user", "content": "hi"}],
                request_id="req-stream-no-midflight-fallback",
            ):
                chunks.append(chunk)

        assert any("partial" in chunk for chunk in chunks)
        assert not any("backup" in chunk for chunk in chunks)

    @pytest.mark.asyncio
    async def test_stream_does_not_resolve_after_nonretryable_error(self):
        """Pre-content stream 4xx errors surface directly instead of re-solving."""
        primary = _make_adapter(
            provider="primary",
            provider_type="on_demand",
            endpoint_id="test-model:primary",
        )
        backup = _make_adapter(
            provider="backup",
            provider_type="on_demand",
            endpoint_id="test-model:backup",
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(primary, 0.5), (backup, 0.5)])
        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

        async def _primary_stream(*args, **kwargs):
            raise _StatusError(400, "bad stream request")
            yield

        async def _backup_stream(*args, **kwargs):
            yield 'data: {"choices":[{"delta":{"content":"backup"}}]}\n\n'
            yield "data: [DONE]\n\n"

        def _choose(candidates, _solution):
            by_id = {candidate.endpoint_id: candidate for candidate in candidates}
            return by_id.get("test-model:primary") or by_id["test-model:backup"]

        primary.stream_chat_completion = _primary_stream
        backup.stream_chat_completion = _backup_stream
        router._sample_solution = _choose  # type: ignore[method-assign]

        with pytest.raises(_StatusError, match="bad stream request") as exc_info:
            async for _ in router.stream_chat_completion(
                "test-model",
                [{"role": "user", "content": "hi"}],
                request_id="req-stream-nonretryable",
            ):
                pass

        routing = getattr(exc_info.value, "_routing", None)
        assert routing is not None
        assert routing["routewise"]["fallback_attempts"] == 0
        assert routing["routewise"]["fallback_policy"] is None

    @pytest.mark.asyncio
    async def test_stream_retry_exhaustion_keeps_last_decision_metadata(self):
        """Pre-content retry exhaustion carries the last stream decision trace."""
        primary = _make_adapter(
            provider="primary",
            provider_type="on_demand",
            endpoint_id="test-model:primary",
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(primary, 1.0)])
        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())

        async def _failed_stream(*args, **kwargs):
            raise _StatusError(503, "stream unavailable")
            yield

        primary.stream_chat_completion = _failed_stream
        with pytest.raises(_StatusError, match="stream unavailable") as exc_info:
            async for _ in router.stream_chat_completion(
                "test-model",
                [{"role": "user", "content": "hi"}],
                request_id="req-stream-exhausted",
            ):
                pass

        routing = exc_info.value._routing
        routewise = routing["routewise"]
        assert routewise["selected_endpoint"] == "test-model:primary"
        assert routewise["fallback_policy"] == "routewise_resolve"
        assert routewise["fallback_attempts"] == 1
        assert routewise["fallback_excluded_endpoints"] == ["test-model:primary"]

    @pytest.mark.asyncio
    async def test_error_does_not_create_router_global_decision_state(self):
        """Failed requests keep decision metadata local to their exception."""
        router, quota, api = _make_router_with_quota_and_api()

        # Both adapters fail
        quota.chat_completion = AsyncMock(side_effect=RuntimeError("fail"))
        api.chat_completion = AsyncMock(side_effect=RuntimeError("fail"))

        with pytest.raises(RuntimeError) as exc_info:
            await router.chat_completion(
                "test-model",
                [{"role": "user", "content": "hi"}],
                request_id="req-error-test",
            )

        assert not hasattr(router, "_pending_decisions")
        assert "routewise" in exc_info.value._routing

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
            event_sink=router._health_registry,
        )

        request_id = "req-backup-test"
        metadata = {
            "selected_provider_type": "on_demand",
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
        decision = _unreserved_decision(hedged, metadata=metadata)
        decision.trace.request_id = request_id

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
            decision,
            "test-model",
            [{"role": "user", "content": "hi"}],
            request_id=request_id,
        )

        # HedgedAdapter should have swapped config -> backup won
        assert hedged.config is backup_adapter.config
        assert decision.metadata["backup_won"] is True

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
        assert rw["selected_provider_type"] in ("quota", "on_demand")
        assert "v_t" in rw

        assert not hasattr(router, "_pending_decisions")

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
        assert routing["routewise"]["selected_provider_type"] in ("quota", "on_demand")

    @pytest.mark.asyncio
    async def test_quota_committed_always_zero(self):
        """quota_committed is 0.0 for all tiers (v_t lives in its own field)."""
        router, _conc, _quota, _api = _make_router_with_all_tiers()

        # S_C path
        decision_sc = router._select_decision("test-model", {"request_id": "req-sc-qc"})
        assert decision_sc.metadata["quota_committed"] == 0.0

        # S_Q path (quota + API only -- no concurrency route in this fixture)
        router2, _quota2, _api2 = _make_router_with_quota_and_api()
        _warm_envelope(router2, lower=0.0000001, upper=0.001)
        decision_sq = router2._select_decision("test-model", {"request_id": "req-sq-qc"})
        meta_sq = decision_sq.metadata
        if meta_sq["selected_provider_type"] == "quota":
            assert meta_sq["quota_committed"] == 0.0
            assert meta_sq["v_t"] > 0  # v_t is separate

        # S_A path
        config_sa = RouteWiseConfig()
        router3, _quota3, _api3 = _make_router_with_quota_and_api(config=config_sa)
        _warm_envelope(router3, lower=1000.0, upper=10000.0)
        decision_sa = router3._select_decision("test-model", {"request_id": "req-sa-qc"})
        assert decision_sa.metadata["quota_committed"] == 0.0


@pytest.mark.unit
class TestRouteWiseEnvelopeCalibration:
    """``start()`` degrades an uncalibrated quota pool that has a fallback leg,
    and only refuses to run when such a pool is the model's only route."""

    @pytest.mark.asyncio
    async def test_start_degrades_when_quota_pool_has_fallback_leg(self, caplog):
        # test-model has both a quota and an on-demand leg, so an uncalibrated
        # envelope must NOT crash startup: the quota leg is masked at request
        # time and the model serves via on-demand until traffic calibrates it.
        router, _quota, _api = _make_router_with_quota_and_api()
        with caplog.at_level(logging.WARNING):
            try:
                await router.start()
            finally:
                await router.stop()
        assert any("uncalibrated" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_start_raises_when_quota_only_pool_uncalibrated(self):
        # Quota-only model with no fallback leg: an uncalibrated envelope leaves
        # it unroutable, so startup must fail fast.
        quota_adapter = _make_adapter(
            provider_type="quota",
            endpoint_id="quota-only:quota-provider",
            quota={"limit": 5000},
        )
        fr = _FakeRouteTable()
        fr.add("test-model", [(quota_adapter, 1.0)])
        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        _seed_quota_snapshots(router)
        with pytest.raises(EnvelopeNotCalibratedError, match="quota-only"):
            await router.start()

    @pytest.mark.asyncio
    async def test_start_passes_when_quota_pool_has_envelope(self):
        router, _quota, _api = _make_router_with_quota_and_api()
        _warm_envelope(router, lower=0.001, upper=0.5)
        try:
            await router.start()
        finally:
            await router.stop()

    @pytest.mark.asyncio
    async def test_start_passes_for_api_only_models(self):
        """Models without any quota provider don't need a calibrated envelope."""
        api_only = _make_adapter(provider_type="on_demand")
        fr = _FakeRouteTable()
        fr.add("test-model", [(api_only, 1.0)])
        router = RouteWiseRouter(route_table=fr, config=RouteWiseConfig())
        try:
            await router.start()
        finally:
            await router.stop()

    def test_quota_provider_is_skipped_when_envelope_uncalibrated(self):
        """Defense in depth: if envelope is uncalibrated at request time,
        quota providers are masked rather than priced on a fabricated shadow.
        """
        router, _quota, api = _make_router_with_quota_and_api()
        # No _warm_envelope call: snapshot returns None and quota is skipped.
        selected = router._select_decision("test-model", {"prompt_tokens": 100}).adapter
        assert selected is api


class TestUpstreamPriorityIsNotPublished:
    """RouteWise leaves the upstream's own scheduling priority alone, on purpose.

    A priority ranks a request by the *un-cached* prefill it imposes, and that
    discount lives in FixedRouter's PrefillLoadTracker, which this router does
    not own or feed. Publishing the raw prompt size instead would stamp every
    warm long-context continuation as an elephant and have an sglang backend
    schedule it last and preempt it -- worse than publishing nothing, which
    leaves those models on the upstream's own default. Pinned here so the
    omission stays a decision rather than becoming a silent regression.
    """

    @pytest.mark.unit
    @pytest.mark.asyncio
    @pytest.mark.parametrize("operation", ("chat", "stream"))
    async def test_dispatch_publishes_no_priority(self, operation):
        from serving.utils import context as req_ctx

        seen: list[Any] = []
        adapter = _make_adapter(provider="sglang", endpoint_id="test-model:local-8003")
        adapter.reports_leg_outcomes = False

        async def _chat(messages, **params):
            seen.append(req_ctx.get().get(req_ctx.UPSTREAM_PRIORITY))
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        async def _stream(messages, **params):
            seen.append(req_ctx.get().get(req_ctx.UPSTREAM_PRIORITY))
            yield json.dumps({"choices": [{"delta": {"content": "ok"}}]})

        adapter.chat_completion = _chat
        adapter.stream_chat_completion = _stream
        router = RouteWiseRouter(config=RouteWiseConfig(db_bootstrap_enabled=False))

        messages = [{"role": "user", "content": "hi"}]
        if operation == "chat":
            await router._execute_adapter(_unreserved_decision(adapter), "test-model", messages)
        else:
            async for _chunk in router._execute_stream_adapter(
                _unreserved_decision(adapter), "test-model", messages
            ):
                pass

        # The dispatch ran (so the assertion is not vacuous) and carried no
        # priority: an sglang backend serves these at its own default.
        assert seen == [None]
