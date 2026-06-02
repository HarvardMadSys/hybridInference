"""Tests for RouteWise session-scoped prefix-cache primitives and routing hooks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from routing.routers import RoutingObservation
from routing.routewise.candidates import CandidatePricing
from routing.routewise.config import RouteWiseConfig
from routing.routewise.prefix_cache import (
    Block,
    CacheAwareCostEstimator,
    CacheScope,
    CacheSignal,
    PrefixCacheShadow,
    SessionProviderPrefixMemory,
    build_blocks,
    canonicalize_prompt,
    longest_common_prefix_tokens,
    price_delta_per_token,
)
from routing.routewise.router import FeasibleProviderCandidate, RouteWiseRouter
from serving.utils import context as req_ctx

SECRET = b"unit-test-secret"


def _scope(session: str = "s1", provider: str = "p1") -> CacheScope:
    return CacheScope(
        user_hash="u1",
        project_hash="proj1",
        session_hash=session,
        provider_id=provider,
        endpoint_id=f"{provider}:host:443",
        model_profile="m1",
        key_slot_id="k1",
    )


def _blocks(*pairs: tuple[str, int]) -> tuple[Block, ...]:
    return tuple(Block(digest=digest, token_count=n) for digest, n in pairs)


def _chars(text: str) -> list[int]:
    return [ord(c) for c in text]


@pytest.mark.unit
class TestBuildBlocks:
    def test_deterministic_for_same_input(self):
        messages = [{"role": "system", "content": "you are helpful"}]
        a = build_blocks(messages, secret=SECRET, block_size=8, tokenize=_chars)
        b = build_blocks(messages, secret=SECRET, block_size=8, tokenize=_chars)
        assert a == b
        assert all(isinstance(block.digest, str) for block in a)

    def test_secret_changes_digest_not_token_count(self):
        messages = [{"role": "user", "content": "hello world"}]
        a = build_blocks(messages, secret=b"secret-a", block_size=8, tokenize=_chars)
        b = build_blocks(messages, secret=b"secret-b", block_size=8, tokenize=_chars)
        assert [blk.token_count for blk in a] == [blk.token_count for blk in b]
        assert [blk.digest for blk in a] != [blk.digest for blk in b]

    def test_block_sizing_keeps_trailing_partial(self):
        blocks = build_blocks(
            [{"role": "user", "content": "x"}],
            secret=SECRET,
            block_size=8,
            tokenize=lambda _text: list(range(20)),
        )
        assert [blk.token_count for blk in blocks] == [8, 8, 4]

    def test_rejects_non_positive_block_size(self):
        with pytest.raises(ValueError, match="block_size"):
            build_blocks([{"role": "user", "content": "x"}], block_size=0, tokenize=_chars)

    def test_append_only_request_shares_leading_prefix(self):
        system = {"role": "system", "content": "S" * 200}
        turn1 = {"role": "user", "content": "first question"}
        turn2 = {"role": "user", "content": "second question"}
        prev = build_blocks([system, turn1], secret=SECRET, block_size=8, tokenize=_chars)
        curr = build_blocks([system, turn1, turn2], secret=SECRET, block_size=8, tokenize=_chars)
        matched = longest_common_prefix_tokens(curr, prev)
        total_prev = sum(blk.token_count for blk in prev)
        assert 0 < matched < total_prev

    def test_canonicalize_is_field_order_insensitive(self):
        a = canonicalize_prompt([{"role": "user", "content": "hi"}], tools=None)
        b = canonicalize_prompt([{"content": "hi", "role": "user"}], tools=None)
        assert a == b


@pytest.mark.unit
class TestLongestCommonPrefixTokens:
    def test_identical_returns_full(self):
        seq = _blocks(("a", 100), ("b", 50))
        assert longest_common_prefix_tokens(seq, seq) == 150

    def test_append_only_returns_shared_prefix(self):
        prev = _blocks(("a", 100), ("b", 100))
        curr = _blocks(("a", 100), ("b", 100), ("c", 100))
        assert longest_common_prefix_tokens(curr, prev) == 200

    def test_first_block_mismatch_returns_zero(self):
        assert longest_common_prefix_tokens(_blocks(("x", 100)), _blocks(("a", 100))) == 0

    def test_empty_returns_zero(self):
        assert longest_common_prefix_tokens((), _blocks(("a", 1))) == 0


@pytest.mark.unit
class TestPriceDelta:
    def test_normal_discount(self):
        assert price_delta_per_token(0.30, 0.03) == pytest.approx(0.27 / 1_000_000.0)

    def test_missing_cached_price_is_no_discount(self):
        assert price_delta_per_token(0.30, None) == 0.0

    def test_zero_cached_price_is_treated_as_not_offered(self):
        # input_cache_reads: "0" must read as "no discount", never "free".
        assert price_delta_per_token(0.30, 0.0) == 0.0

    def test_cached_not_cheaper_is_no_discount(self):
        assert price_delta_per_token(0.30, 0.30) == 0.0
        assert price_delta_per_token(0.30, 0.40) == 0.0

    def test_zero_prompt_price_is_no_discount(self):
        assert price_delta_per_token(0.0, 0.0) == 0.0


@pytest.mark.unit
class TestSessionProviderPrefixMemory:
    def test_cold_lookup_returns_empty_signal(self):
        mem = SessionProviderPrefixMemory(min_match_tokens=10)
        signal = mem.lookup(_scope(), _blocks(("a", 100)), now=0.0)
        assert signal.has_history is False
        assert signal.matched_prefix_tokens == 0
        assert signal.meets_threshold is False
        assert signal.expected_cached_tokens == 0.0

    def test_observe_then_lookup_matches_prefix(self):
        mem = SessionProviderPrefixMemory(min_match_tokens=150)
        scope = _scope()
        mem.observe(scope, _blocks(("a", 100), ("b", 100)), observed_cached_tokens=None, now=0.0)
        signal = mem.lookup(scope, _blocks(("a", 100), ("b", 100), ("c", 50)), now=1.0)
        assert signal.has_history is True
        assert signal.matched_prefix_tokens == 200
        assert signal.meets_threshold is True

    def test_below_threshold_does_not_meet(self):
        mem = SessionProviderPrefixMemory(min_match_tokens=500)
        scope = _scope()
        mem.observe(scope, _blocks(("a", 100)), observed_cached_tokens=10, now=0.0)
        signal = mem.lookup(scope, _blocks(("a", 100)), now=1.0)
        assert signal.matched_prefix_tokens == 100
        assert signal.meets_threshold is False

    def test_ttl_expiry_makes_lookup_cold(self):
        mem = SessionProviderPrefixMemory(ttl_sec=100.0, min_match_tokens=1)
        scope = _scope()
        mem.observe(scope, _blocks(("a", 100)), observed_cached_tokens=10, now=0.0)
        assert mem.lookup(scope, _blocks(("a", 100)), now=50.0).has_history is True
        assert mem.lookup(scope, _blocks(("a", 100)), now=200.0).has_history is False

    def test_lru_evicts_oldest_over_cap(self):
        mem = SessionProviderPrefixMemory(max_entries=2, ttl_sec=0.0, min_match_tokens=1)
        mem.observe(_scope(session="a"), _blocks(("x", 10)), observed_cached_tokens=1, now=1.0)
        mem.observe(_scope(session="b"), _blocks(("x", 10)), observed_cached_tokens=1, now=2.0)
        mem.observe(_scope(session="c"), _blocks(("x", 10)), observed_cached_tokens=1, now=3.0)
        assert len(mem) == 2
        assert mem.lookup(_scope(session="a"), _blocks(("x", 10)), now=4.0).has_history is False
        assert mem.lookup(_scope(session="c"), _blocks(("x", 10)), now=4.0).has_history is True

    def test_observe_classifies_hit_miss_unknown(self):
        mem = SessionProviderPrefixMemory(min_match_tokens=1)
        scope = _scope()
        mem.observe(scope, _blocks(("a", 100)), observed_cached_tokens=None, now=0.0)
        mem.observe(scope, _blocks(("a", 100)), observed_cached_tokens=0, now=1.0)
        mem.observe(scope, _blocks(("a", 100)), observed_cached_tokens=5, now=2.0)
        signal = mem.lookup(scope, _blocks(("a", 100)), now=3.0)
        assert signal.unknown_count == 1
        assert signal.confirmed_miss_count == 1
        assert signal.confirmed_hit_count == 1


@pytest.mark.unit
class TestCacheAwareCostEstimator:
    def _signal(self, **kw) -> CacheSignal:
        base = {
            "matched_prefix_tokens": 1000,
            "meets_threshold": True,
            "has_history": True,
        }
        base.update(kw)
        return CacheSignal(**base)

    def test_applies_matched_prefix_discount(self):
        est = CacheAwareCostEstimator()
        # expected cached tokens = matched prefix (1000); no hit-rate / confidence.
        result = est.adjust(0.01, self._signal(), price_delta=0.27 / 1_000_000.0)
        assert result.expected_cached_tokens == pytest.approx(1000.0)
        assert result.cache_discount == pytest.approx(1000 * 0.27 / 1_000_000.0)
        assert result.adjusted_cost == pytest.approx(0.01 - 1000 * 0.27 / 1_000_000.0)
        assert result.applied is True

    def test_discount_floored_so_cost_never_negative(self):
        est = CacheAwareCostEstimator()
        result = est.adjust(0.01, self._signal(), price_delta=1.0)  # absurd delta
        assert result.cache_discount == pytest.approx(0.01)
        assert result.adjusted_cost == pytest.approx(0.0)

    def test_no_discount_below_threshold(self):
        est = CacheAwareCostEstimator()
        result = est.adjust(
            0.01, self._signal(meets_threshold=False), price_delta=0.27 / 1_000_000.0
        )
        assert result.applied is False
        assert result.adjusted_cost == pytest.approx(0.01)

    def test_no_discount_when_price_delta_zero(self):
        # The minimax-m2.5 OpenRouter leg (cache_reads=0) must be a no-op.
        est = CacheAwareCostEstimator()
        result = est.adjust(0.01, self._signal(), price_delta=0.0)
        assert result.applied is False
        assert result.adjusted_cost == pytest.approx(0.01)

    def test_no_discount_for_cold_signal(self):
        est = CacheAwareCostEstimator()
        result = est.adjust(0.01, CacheSignal.empty(), price_delta=0.27 / 1_000_000.0)
        assert result.applied is False
        assert result.adjusted_cost == pytest.approx(0.01)

    def test_disabled_skips_discount(self):
        est = CacheAwareCostEstimator()
        result = est.adjust(0.01, self._signal(), price_delta=0.27 / 1_000_000.0, enabled=False)
        assert result.applied is False
        assert result.adjusted_cost == pytest.approx(0.01)


@pytest.mark.unit
class TestPrefixCacheShadow:
    def _coord(self) -> PrefixCacheShadow:
        return PrefixCacheShadow(
            enabled=True,
            memory=SessionProviderPrefixMemory(min_match_tokens=1),
            block_size=8,
            secret=SECRET,
            tokenize=_chars,
        )

    def test_scope_hashes_sensitive_fields_only(self):
        scope = self._coord().scope_for(
            session="sess-1",
            provider_id="prov",
            endpoint_id="prov:h:1",
            model_profile="m",
        )
        assert scope.session_hash not in ("", "sess-1")  # hashed, not raw
        assert scope.provider_id == "prov"  # internal label, not hashed
        assert scope.endpoint_id == "prov:h:1"
        assert scope.user_hash == ""  # empty stays empty

    def test_remember_then_evaluate_matches_next_turn(self):
        coord = self._coord()
        msgs1 = [{"role": "system", "content": "S" * 200}, {"role": "user", "content": "q1"}]
        msgs2 = [*msgs1, {"role": "user", "content": "q2"}]
        scope = coord.scope_for(session="s", provider_id="p", endpoint_id="e", model_profile="m")
        first = coord.evaluate(scope, coord.build_blocks(msgs1), cold_cost=0.01, price_delta=1e-7)
        assert first.has_history is False
        coord.remember(scope, coord.build_blocks(msgs1))
        second = coord.evaluate(scope, coord.build_blocks(msgs2), cold_cost=0.01, price_delta=1e-7)
        assert second.has_history is True
        assert second.matched_prefix_tokens > 0

    def test_zero_delta_is_shadow_only(self):
        coord = self._coord()
        scope = coord.scope_for(session="s", provider_id="p", endpoint_id="e", model_profile="m")
        blocks = coord.build_blocks([{"role": "user", "content": "x" * 100}])
        coord.remember(scope, blocks)
        record = coord.evaluate(scope, blocks, cold_cost=0.01, price_delta=0.0)
        assert record.would_apply is False
        assert record.shadow_cache_discount == 0.0


def _fake_candidate(endpoint_id: str, provider: str) -> FeasibleProviderCandidate:
    return FeasibleProviderCandidate(
        endpoint_id=endpoint_id,
        adapter=SimpleNamespace(config=SimpleNamespace(provider=provider)),
        tier="api",
        weight=1.0,
        effective_cost_usd=0.01,
        request_cost_usd=0.01,
        mean_ttft_sec=1.0,
        cost_reason="cold_api_cost",
    )


def _obs(
    endpoint_id: str, *, success: bool = True, cached: int | None = None
) -> RoutingObservation:
    return RoutingObservation(
        model_id="m1",
        endpoint_id=endpoint_id,
        ttft_ms=1.0,
        total_latency_ms=1.0,
        token_count=1,
        success=success,
        quota_committed=0.0,
        cached_input_tokens=cached,
    )


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
            subscription_type="api",
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


def _fixed_router(*adapters: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        routes={"m1": SimpleNamespace(adapters=[(adapter, 1.0) for adapter in adapters])}
    )


@pytest.mark.unit
class TestRouteWiseRouterPrefixCacheShadow:
    def _router(self) -> RouteWiseRouter:
        router = RouteWiseRouter(config=RouteWiseConfig(prefix_cache_shadow_enabled=True))
        router.prefix_cache_shadow = PrefixCacheShadow(
            enabled=True,
            memory=SessionProviderPrefixMemory(min_match_tokens=1),
            block_size=8,
            secret=SECRET,
            tokenize=_chars,
        )
        router.route_candidates["m1"] = [
            SimpleNamespace(
                endpoint_id="prov-a:h:1",
                pricing=CandidatePricing(prompt=0.30, cache_read=0.03),
            ),
            SimpleNamespace(
                endpoint_id="prov-b:h:1",
                pricing=CandidatePricing(prompt=0.30, cache_read=0.03),
            ),
        ]
        return router

    def _route(self, router, request_id, messages, *, affinity="userA", session="sess-1"):
        # Mirror the real flow: req_ctx carries the EXTERNAL id, while the router
        # sees only an INTERNAL decision id in params/context (different value).
        # The stash must key off the external id so the observation can find it.
        internal_id = f"internal-{request_id}"
        req_ctx.set({"request_id": request_id, "affinity_key": affinity})
        router._pending_decisions[internal_id] = {}
        cands = [_fake_candidate("prov-a:h:1", "prov-a"), _fake_candidate("prov-b:h:1", "prov-b")]
        ctx = {
            "messages": messages,
            "params": {"session_id": session},
            "request_id": internal_id,
        }
        router._record_prefix_cache_shadow("m1", ctx, cands, internal_id)
        return router._pending_decisions[internal_id]["prefix_cache_shadow"]

    @staticmethod
    def _observe(router, request_id, endpoint_id, *, success=True, cached=None):
        req_ctx.set({"request_id": request_id, "affinity_key": "ignored-at-observe"})
        router._commit_prefix_cache_observation(_obs(endpoint_id, success=success, cached=cached))

    def test_flag_defaults_off(self):
        router = RouteWiseRouter(config=RouteWiseConfig())
        assert router.prefix_cache_shadow.enabled is False

    def test_matched_only_after_selected_success(self):
        router = self._router()
        assert self._route(router, "r1", _MSGS1)["prov-a:h:1"]["matched_prefix_tokens"] == 0
        self._observe(router, "r1", "prov-a:h:1")  # prov-a actually served
        recs = self._route(router, "r2", _MSGS2)
        assert recs["prov-a:h:1"]["matched_prefix_tokens"] > 0
        assert recs["prov-b:h:1"]["matched_prefix_tokens"] == 0  # never served

    def test_failed_attempt_does_not_warm(self):
        router = self._router()
        self._route(router, "r1", _MSGS1)
        self._observe(router, "r1", "prov-a:h:1", success=False)  # primary failed
        assert "r1" not in router._prefix_cache_pending
        recs = self._route(router, "r2", _MSGS2)
        assert recs["prov-a:h:1"]["matched_prefix_tokens"] == 0

    @pytest.mark.asyncio
    async def test_stale_pending_decision_sweep_removes_prefix_stash(self):
        router = self._router()
        req_ctx.set({"request_id": "r1", "affinity_key": "userA"})
        router._pending_decisions["r1"] = {"timestamp": -1_000_000_000.0}
        cands = [_fake_candidate("prov-a:h:1", "prov-a")]
        ctx = {
            "messages": _MSGS1,
            "params": {"session_id": "sess-1"},
            "request_id": "r1",
        }
        router._record_prefix_cache_shadow("m1", ctx, cands, "r1")
        assert "r1" in router._prefix_cache_pending

        evicted = await router._sweep_pending_decisions_once()

        assert evicted == 1
        assert "r1" not in router._prefix_cache_pending

    def test_remembers_winner_not_primary(self):
        router = self._router()
        self._route(router, "r1", _MSGS1)
        self._observe(router, "r1", "prov-b:h:1")  # backup won the hedge / fallback
        recs = self._route(router, "r2", _MSGS2)
        assert recs["prov-b:h:1"]["matched_prefix_tokens"] > 0  # the real winner is warm
        assert recs["prov-a:h:1"]["matched_prefix_tokens"] == 0

    def test_different_user_same_session_is_isolated(self):
        router = self._router()
        self._route(router, "r1", _MSGS1, affinity="userA", session="sess-1")
        self._observe(router, "r1", "prov-a:h:1")
        # User B reuses the SAME session id but a different affinity key.
        recs = self._route(router, "r2", _MSGS2, affinity="userB", session="sess-1")
        assert recs["prov-a:h:1"]["matched_prefix_tokens"] == 0

    def test_observed_cached_tokens_reach_memory(self):
        router = self._router()
        self._route(router, "r1", _MSGS1, affinity="userA", session="sess-1")
        self._observe(router, "r1", "prov-a:h:1", cached=123)
        scope = router.prefix_cache_shadow.scope_for(
            session="sess-1",
            provider_id="prov-a",
            endpoint_id="prov-a:h:1",
            model_profile="m1",
            user="userA",
            cache_params="{}",
        )
        signal = router.prefix_cache_shadow.memory.lookup(scope, ())
        assert signal.confirmed_hit_count == 1
        assert signal.confirmed_miss_count == 0

    def test_no_session_id_skips_recording(self):
        router = self._router()
        req_ctx.set({"request_id": "r1", "affinity_key": "userA"})
        router._pending_decisions["r1"] = {}
        cands = [_fake_candidate("prov-a:h:1", "prov-a")]
        ctx = {"messages": [{"role": "user", "content": "x"}], "params": {}, "request_id": "r1"}
        router._record_prefix_cache_shadow("m1", ctx, cands, "r1")
        assert "prefix_cache_shadow" not in router._pending_decisions["r1"]


@pytest.mark.unit
class TestRouteWiseRouterPrefixCacheCostAdjustment:
    def _router(
        self,
        *,
        cost_adjustment: bool,
        warm_api_keys: list[str] | None = None,
    ) -> tuple[RouteWiseRouter, SimpleNamespace, SimpleNamespace]:
        cold_cheaper = _api_adapter(
            "prov-a",
            "prov-a:h:1",
            prompt_price=0.30,
            cache_read_price=0.03,
        )
        warm_slightly_pricier = _api_adapter(
            "prov-b",
            "prov-b:h:1",
            prompt_price=0.31,
            cache_read_price=0.03,
            api_keys=warm_api_keys,
        )
        router = RouteWiseRouter(
            fixed_router=_fixed_router(cold_cheaper, warm_slightly_pricier),
            config=RouteWiseConfig(
                budget_alpha=0.0,
                prefix_cache_shadow_enabled=True,
                prefix_cache_cost_adjustment_enabled=cost_adjustment,
            ),
        )
        router.prefix_cache_shadow = PrefixCacheShadow(
            enabled=True,
            memory=SessionProviderPrefixMemory(min_match_tokens=1),
            block_size=8,
            secret=SECRET,
            tokenize=_chars,
        )
        return router, cold_cheaper, warm_slightly_pricier

    @staticmethod
    def _warm_provider_b(router: RouteWiseRouter) -> None:
        blocks = router.prefix_cache_shadow.build_blocks(_MSGS1)
        scope = router.prefix_cache_shadow.scope_for(
            session="sess-1",
            provider_id="prov-b",
            endpoint_id="prov-b:h:1",
            model_profile="m1",
            user="userA",
            cache_params="{}",
        )
        router.prefix_cache_shadow.remember(scope, blocks)

    @staticmethod
    def _select(router: RouteWiseRouter):
        req_ctx.set({"request_id": "r-select", "affinity_key": "userA"})
        return router._select_adapter(
            "m1",
            {
                "request_id": "r-select",
                "messages": _MSGS2,
                "params": {
                    "session_id": "sess-1",
                    "prompt_tokens": 1000,
                },
            },
        )

    def test_shadow_only_does_not_change_real_route_selection(self):
        router, cold_cheaper, _warm_slightly_pricier = self._router(cost_adjustment=False)
        self._warm_provider_b(router)

        selected = self._select(router)

        assert selected is cold_cheaper
        meta = router._pending_decisions["r-select"]
        assert meta["candidate_cost_reasons"]["prov-b:h:1"] == "cold_api_cost"
        assert meta["candidate_costs_usd"]["prov-b:h:1"] == pytest.approx(
            meta["candidate_request_costs_usd"]["prov-b:h:1"]
        )
        assert "prov-b:h:1" not in meta["candidate_prefix_cache_discounts_usd"]

    def test_cost_adjustment_indirectly_changes_selection_via_effective_cost(self):
        router, _cold_cheaper, warm_slightly_pricier = self._router(cost_adjustment=True)
        self._warm_provider_b(router)

        selected = self._select(router)

        assert selected is warm_slightly_pricier
        meta = router._pending_decisions["r-select"]
        assert meta["candidate_cost_reasons"]["prov-b:h:1"] == "prefix_cache_adjusted_api_cost"
        assert meta["candidate_prefix_cache_discounts_usd"]["prov-b:h:1"] > 0
        assert meta["candidate_prefix_cache_expected_tokens"]["prov-b:h:1"] > 0
        assert meta["candidate_costs_usd"]["prov-b:h:1"] < meta["candidate_request_costs_usd"][
            "prov-b:h:1"
        ]
        assert meta["primary_routing_estimated_cost_usd"] == pytest.approx(
            meta["selected_effective_cost_usd"]
        )

    def test_cost_adjustment_skips_rotating_key_pools_without_key_slot(self):
        router, cold_cheaper, _warm_slightly_pricier = self._router(
            cost_adjustment=True,
            warm_api_keys=["key-a", "key-b"],
        )
        self._warm_provider_b(router)

        selected = self._select(router)

        assert selected is cold_cheaper
        meta = router._pending_decisions["r-select"]
        assert meta["candidate_cost_reasons"]["prov-b:h:1"] == "cold_api_cost"
        assert "prov-b:h:1" not in meta["candidate_prefix_cache_discounts_usd"]
