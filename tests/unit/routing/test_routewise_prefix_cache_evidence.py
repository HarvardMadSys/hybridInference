"""Evidence-aware prefix-cache locality tests.

These tests prove that HybridInference's RouteWise prefix-cache cost model
distinguishes three facts:

  1. Prefix opportunity  -- the current request shares reusable prefix content
     with prior traffic (determined by HMAC block-prefix matching).
  2. Potential warming   -- a successful request reached a destination and may
     have populated its cache.  This is NOT strong routing evidence.
  3. Verified reuse      -- provider usage explicitly reports non-zero cache
     reuse.  This IS positive locality evidence.

The bug under test: successful dispatch alone was sufficient to populate the
locality model, so a later matching prefix could receive a cache discount even
though the provider never demonstrated reuse.

The failing baseline (test_failing_baseline_*) asserts the correct behavior.
Before the fix, these tests FAIL because cached_tokens=0 still warms a full
discount. After the fix, they PASS.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from routing.route_table import EffectiveRoute
from routing.routers import RoutingObservation
from routing.routewise.config import RouteWiseConfig
from routing.routewise.prefix_cache import (
    PrefixCacheCoordinator,
    SessionProviderPrefixMemory,
    price_delta_per_token,
)
from routing.routewise.router import RouteWiseRouter
from serving.utils import context as req_ctx

SECRET = b"unit-test-secret"


def _chars(text: str) -> list[int]:
    return [ord(c) for c in text]


# ---------------------------------------------------------------------------
# Reusable test fixtures (mirroring test_routewise_prefix_cache.py)
# ---------------------------------------------------------------------------

_SYS = {"role": "system", "content": "S" * 200}
_MSGS1 = [_SYS, {"role": "user", "content": "q1"}]
_MSGS2 = [_SYS, {"role": "user", "content": "q1"}, {"role": "user", "content": "q2"}]


def _api_adapter(
    provider: str,
    endpoint_id: str,
    *,
    prompt_price: float,
    cache_read_price: float,
    api_keys: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            id="m1",
            provider=provider,
            provider_type="on_demand",
            endpoint_id=endpoint_id,
            base_url=f"https://{provider}.example/v1",
            pricing={
                "prompt": str(prompt_price),
                "completion": "0",
                "input_cache_reads": str(cache_read_price),
            },
            api_keys=api_keys,
        )
    )


class _StaticRouteTable:
    def __init__(self, adapters: tuple[SimpleNamespace, ...]) -> None:
        self._adapters = tuple((adapter, 1.0) for adapter in adapters)

    def iter_effective_routes(self) -> tuple[EffectiveRoute, ...]:
        return (
            EffectiveRoute(
                route_key="m1",
                canonical_model_id="m1",
                adapters=self._adapters,
            ),
        )

    def canonical_id(self, model_id: str) -> str:
        return model_id


def _route_table(*adapters: SimpleNamespace) -> _StaticRouteTable:
    return _StaticRouteTable(adapters)


def _obs(
    endpoint_id: str,
    *,
    request_id: str,
    success: bool = True,
    terminal: bool = True,
    cached_tokens: int | None = None,
) -> RoutingObservation:
    return RoutingObservation(
        model_id="m1",
        endpoint_id=endpoint_id,
        ttft_ms=1.0,
        total_latency_ms=1.0,
        token_count=1,
        success=success,
        request_id=request_id,
        terminal=terminal,
        cached_tokens=cached_tokens,
        strategy_metadata={"routewise": {"quota_committed": 0.0}},
    )


@pytest.mark.unit
class TestEvidenceAwarePrefixCacheFailingBaseline:
    """Before the fix: success + cached_tokens=0 creates a false locality discount.

    Setup:
      prov-a : expensive cold ($0.30/1M prompt), cheap cache ($0.03/1M) -> only
               preferable if cache reuse is VERIFIED.
      prov-b : slightly cheaper cold ($0.25/1M prompt), no cache discount.

    Cold truth: prov-b wins (0.25 < 0.30).
    Bug: prov-a returns cached_tokens=0, but the discount is still applied to
         the next matching request, so prov-a incorrectly wins.
    """

    def _router(self) -> tuple[RouteWiseRouter, SimpleNamespace, SimpleNamespace]:
        expensive_cold = _api_adapter(
            "prov-a", "prov-a:h:1", prompt_price=0.30, cache_read_price=0.03
        )
        cheap_cold = _api_adapter("prov-b", "prov-b:h:1", prompt_price=0.25, cache_read_price=0.25)
        router = RouteWiseRouter(
            route_table=_route_table(expensive_cold, cheap_cold),
            config=RouteWiseConfig(
                budget_alpha=0.0,
                prefix_cache_cost_adjustment_enabled=True,
            ),
        )
        router.prefix_cache = PrefixCacheCoordinator(
            enabled=True,
            memory=SessionProviderPrefixMemory(min_match_tokens=1),
            block_size=8,
            secret=SECRET,
            tokenize=_chars,
        )
        return router, expensive_cold, cheap_cold

    def _scope(self, router: RouteWiseRouter, provider: str, endpoint: str):
        return router.prefix_cache.scope_for(
            session="sess-1",
            provider_id=provider,
            endpoint_id=endpoint,
            model_profile="m1",
            user="userA",
            cache_params="{}",
        )

    def _stash_commit(
        self,
        router: RouteWiseRouter,
        request_id: str,
        endpoint_id: str,
        cached_tokens: int | None,
        *,
        success: bool = True,
    ) -> None:
        """Simulate: route-time stash + post-success observation commit."""
        provider = endpoint_id.replace(":h:1", "")
        scope = self._scope(router, provider, endpoint_id)
        blocks = router.prefix_cache.build_blocks(_MSGS1)
        router._stash_prefix_for_commit((blocks, {"scopes": {endpoint_id: scope}}), request_id)
        router._commit_prefix_cache_observation(
            _obs(
                endpoint_id,
                request_id=request_id,
                success=success,
                terminal=success,
                cached_tokens=cached_tokens,
            )
        )

    def _select(self, router: RouteWiseRouter):
        req_ctx.set({"request_id": "r-select", "affinity_key": "userA"})
        return router._select_decision(
            "m1",
            {
                "request_id": "r-select",
                "messages": _MSGS2,
                "params": {"session_id": "sess-1", "prompt_tokens": 1000},
            },
        )

    # --- Failing baseline ---------------------------------------------------

    def test_request1_miss_does_not_create_false_discount(self) -> None:
        """Request 1 to prov-a returns cached_tokens=0. Next request: no discount."""
        router, expensive_cold, cheap_cold = self._router()

        # Request 1: prov-a served, reported NO cache reuse
        self._stash_commit(router, "r1", "prov-a:h:1", cached_tokens=0)

        # Request 2: same session, overlapping prefix
        decision = self._select(router)
        meta = decision.metadata

        # B must win: A showed no verified reuse, so no cache discount may apply.
        assert decision.adapter is cheap_cold, (
            "B should win: prov-a reported cached_tokens=0 (no verified reuse), "
            "so no cache discount may lower its effective cost. "
            f"Got {decision.adapter.config.provider}."
        )
        # A must NOT carry a prefix-cache discount.
        assert meta["candidate_cost_reasons"]["prov-a:h:1"] == "cold_api_cost"
        assert "prov-a:h:1" not in meta["candidate_prefix_cache_discounts_usd"]

    def test_request1_unknown_evidence_no_false_discount(self) -> None:
        """Request 1 to prov-a returns cached_tokens=None. Next request: no discount."""
        router, expensive_cold, cheap_cold = self._router()

        # Request 1: prov-a served, cache usage UNKNOWN
        self._stash_commit(router, "r1", "prov-a:h:1", cached_tokens=None)

        # Request 2
        decision = self._select(router)
        meta = decision.metadata

        assert decision.adapter is cheap_cold, (
            "B should win: cached_tokens=None is not evidence of reuse."
        )
        assert meta["candidate_cost_reasons"]["prov-a:h:1"] == "cold_api_cost"

    # --- Positive control ---------------------------------------------------

    def test_positive_hit_creates_verified_discount(self) -> None:
        """Request 1 to prov-a returns cached_tokens>0. Next request: A wins."""
        router, expensive_cold, cheap_cold = self._router()

        # Request 1: prov-a served, reported verified cache reuse
        self._stash_commit(router, "r1", "prov-a:h:1", cached_tokens=750)

        # Request 2: same session, overlapping prefix
        decision = self._select(router)
        meta = decision.metadata

        # A must now win: verified reuse makes A's effective cost lower.
        assert decision.adapter is expensive_cold, (
            "A should win: cached_tokens=750 is verified reuse, so the "
            "cache discount legitimately lowers its effective cost."
        )
        assert meta["candidate_cost_reasons"]["prov-a:h:1"] == "prefix_cache_adjusted_api_cost"
        assert meta["candidate_prefix_cache_discounts_usd"]["prov-a:h:1"] > 0

    def test_positive_then_negative_reduces_confidence(self) -> None:
        """Positive hit gives A a discount; subsequent zero reduces it."""
        router, expensive_cold, cheap_cold = self._router()

        # Request 1: verified reuse
        self._stash_commit(router, "r1", "prov-a:h:1", cached_tokens=800)
        # Request 2: should now prefer A
        d2 = self._select(router)
        assert d2.adapter is expensive_cold

        # Request 3: A reports miss
        self._stash_commit(router, "r3", "prov-a:h:1", cached_tokens=0)
        # Request 4: confidence reduced; B should win again
        d4 = self._select(router)
        assert d4.adapter is cheap_cold, (
            "After a miss, A's confidence should drop and B should win again."
        )

    def test_negative_then_positive_recovers(self) -> None:
        """A miss then a hit: locality can recover."""
        router, expensive_cold, cheap_cold = self._router()

        # Request 1: miss
        self._stash_commit(router, "r1", "prov-a:h:1", cached_tokens=0)
        # Request 2: B wins (no evidence)
        assert self._select(router).adapter is cheap_cold

        # Request 3: verified hit
        self._stash_commit(router, "r3", "prov-a:h:1", cached_tokens=800)
        # Request 4: A wins again
        assert self._select(router).adapter is expensive_cold

    def test_zero_does_not_permanently_blacklist(self) -> None:
        """A string of misses must not permanently prevent future reuse."""
        router, expensive_cold, cheap_cold = self._router()

        # Many misses
        for i in range(5):
            self._stash_commit(router, f"r-miss-{i}", "prov-a:h:1", cached_tokens=0)

        # But then a verified hit
        self._stash_commit(router, "r-hit", "prov-a:h:1", cached_tokens=800)
        # A should win
        assert self._select(router).adapter is expensive_cold


@pytest.mark.unit
class TestEvidenceAwarePrefixCacheIsolation:
    """Session, endpoint, and TTL isolation."""

    def _router(self) -> RouteWiseRouter:
        expensive_cold = _api_adapter(
            "prov-a", "prov-a:h:1", prompt_price=0.30, cache_read_price=0.03
        )
        cheap_cold = _api_adapter("prov-b", "prov-b:h:1", prompt_price=0.10, cache_read_price=0.10)
        router = RouteWiseRouter(
            route_table=_route_table(expensive_cold, cheap_cold),
            config=RouteWiseConfig(
                budget_alpha=0.0,
                prefix_cache_cost_adjustment_enabled=True,
            ),
        )
        router.prefix_cache = PrefixCacheCoordinator(
            enabled=True,
            memory=SessionProviderPrefixMemory(min_match_tokens=1),
            block_size=8,
            secret=SECRET,
            tokenize=_chars,
        )
        return router

    def _scope(self, router, session="sess-1", provider="prov-a", endpoint="prov-a:h:1"):
        return router.prefix_cache.scope_for(
            session=session,
            provider_id=provider,
            endpoint_id=endpoint,
            model_profile="m1",
            user="userA",
            cache_params="{}",
        )

    def _stash_commit(self, router, request_id, endpoint_id, cached_tokens):
        provider = endpoint_id.replace(":h:1", "")
        scope = self._scope(router, provider=provider, endpoint=endpoint_id)
        blocks = router.prefix_cache.build_blocks(_MSGS1)
        router._stash_prefix_for_commit((blocks, {"scopes": {endpoint_id: scope}}), request_id)
        router._commit_prefix_cache_observation(
            _obs(endpoint_id, request_id=request_id, cached_tokens=cached_tokens)
        )

    def _select(self, router, session="sess-1"):
        req_ctx.set({"request_id": "r-select", "affinity_key": "userA"})
        return router._select_decision(
            "m1",
            {
                "request_id": "r-select",
                "messages": _MSGS2,
                "params": {"session_id": session, "prompt_tokens": 1000},
            },
        )

    def test_different_session_isolated(self) -> None:
        """Cache evidence for session X must not warm session Y."""
        router = self._router()

        # Warm prov-a for session-1 with verified reuse
        scope1 = self._scope(router, session="session-1")
        router.prefix_cache.remember(scope1, router.prefix_cache.build_blocks(_MSGS1))
        router.prefix_cache.record_evidence(scope1, cached_tokens=800)

        # Different session: no evidence
        scope2 = self._scope(router, session="session-2")
        router.prefix_cache.remember(scope2, router.prefix_cache.build_blocks(_MSGS1))
        # Do NOT record evidence for scope2

        decision = self._select(router, session="session-2")
        # prov-b (cheap cold) must win for session-2
        meta = decision.metadata
        assert meta["candidate_cost_reasons"]["prov-a:h:1"] == "cold_api_cost"

    def test_different_endpoint_isolated(self) -> None:
        """Evidence for endpoint A must not warm endpoint B."""
        router = self._router()

        # Warm prov-a endpoint
        scope_a = self._scope(router, endpoint="prov-a:h:1")
        router.prefix_cache.remember(scope_a, router.prefix_cache.build_blocks(_MSGS1))
        router.prefix_cache.record_evidence(scope_a, cached_tokens=800)

        # prov-b endpoint must remain cold
        scope_b = self._scope(router, provider="prov-b", endpoint="prov-b:h:1")
        router.prefix_cache.remember(scope_b, router.prefix_cache.build_blocks(_MSGS1))

        decision = self._select(router)
        meta = decision.metadata
        # prov-a gets discount (verified), prov-b does not
        assert meta["candidate_cost_reasons"]["prov-a:h:1"] == "prefix_cache_adjusted_api_cost"
        assert meta["candidate_cost_reasons"]["prov-b:h:1"] == "cold_api_cost"


@pytest.mark.unit
class TestEvidenceAwareFallbackAndHedge:
    """Evidence attribution for fallback and hedge winners."""

    def _router(self) -> tuple[RouteWiseRouter, SimpleNamespace, SimpleNamespace]:
        expensive_cold = _api_adapter(
            "prov-a", "prov-a:h:1", prompt_price=0.30, cache_read_price=0.03
        )
        cheap_cold = _api_adapter("prov-b", "prov-b:h:1", prompt_price=0.10, cache_read_price=0.10)
        router = RouteWiseRouter(
            route_table=_route_table(expensive_cold, cheap_cold),
            config=RouteWiseConfig(
                budget_alpha=0.0,
                prefix_cache_cost_adjustment_enabled=True,
            ),
        )
        router.prefix_cache = PrefixCacheCoordinator(
            enabled=True,
            memory=SessionProviderPrefixMemory(min_match_tokens=1),
            block_size=8,
            secret=SECRET,
            tokenize=_chars,
        )
        return router, expensive_cold, cheap_cold

    def _scope(self, router, provider, endpoint):
        return router.prefix_cache.scope_for(
            session="sess-1",
            provider_id=provider,
            endpoint_id=endpoint,
            model_profile="m1",
            user="userA",
            cache_params="{}",
        )

    def test_fallback_evidence_attributed_to_winner(self) -> None:
        """If A fails and B wins, only B may receive positive cache evidence."""
        router, expensive_cold, cheap_cold = self._router()

        scope_a = self._scope(router, "prov-a", "prov-a:h:1")
        scope_b = self._scope(router, "prov-b", "prov-b:h:1")
        blocks = router.prefix_cache.build_blocks(_MSGS1)

        # Stash BOTH eligible scopes, then A fails, B succeeds
        router._stash_prefix_for_commit(
            (blocks, {"scopes": {"prov-a:h:1": scope_a, "prov-b:h:1": scope_b}}),
            "r1",
        )
        # A fails (non-terminal -- keeps stash for winner)
        router._commit_prefix_cache_observation(
            _obs("prov-a:h:1", request_id="r1", success=False, terminal=False)
        )
        # B wins with verified reuse
        router._commit_prefix_cache_observation(
            _obs("prov-b:h:1", request_id="r1", cached_tokens=500)
        )

        # Now scope_b should have evidence, scope_a should not
        rec_b = router.prefix_cache.evaluate(
            scope_b,
            router.prefix_cache.build_blocks(_MSGS2),
            cold_cost=0.01,
            price_delta=price_delta_per_token(0.10, 0.10),
        )
        # prov-b has no cache price discount (0.10 == 0.10 -> delta 0), so even
        # with evidence the discount is zero. Check evidence state instead via
        # a provider that DOES offer a discount.
        rec_a = router.prefix_cache.evaluate(
            scope_a,
            router.prefix_cache.build_blocks(_MSGS2),
            cold_cost=0.01,
            price_delta=price_delta_per_token(0.30, 0.03),
        )
        # A must NOT have evidence from B's success
        assert rec_a.would_apply is False, "A failed and must not receive B's cache evidence."
