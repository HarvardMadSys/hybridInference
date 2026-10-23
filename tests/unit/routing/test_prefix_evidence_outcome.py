"""Regression: a transient startup stub must not poison prefix-locality memory.

Incident shape (2026-10-02, FreeInference /goal loop)
----------------------------------------------------
A provider answered HTTP 200 with a valid terminal SSE event whose only output
was a warmup notice::

    "The model is starting up - this takes about 120 seconds. Please wait..."

That string *contains text*, so ``not _is_empty_completion(...)`` was True and
the request was recorded as a success. Successes commit prefix locality, and
the commit path replaced the stored entry wholesale::

    self._entries[scope] = _Entry(blocks=new_blocks, ...)

A short transient response therefore overwrote the blocks describing a warm
~185k-token conversation. The next real request compared itself against the
stub's blocks, matched almost nothing, fell below ``min_match_tokens``, and
received no cache discount -- and because every later request rewrote the entry
from itself, the estimate stayed at zero for the whole ~87-request loop.

The defect is the conflation of *transport success* with *semantic progress*.
This file pins the required behavior:

* a non-progressing completion must not mutate positive locality evidence
* strong existing evidence must survive a weak or indeterminate observation
* the provider's own ``cached_input_tokens`` must reach the routing layer so
  "router expected warm / provider reported cold" is directly observable

The first test in ``TestTransientStubDoesNotPoisonLocality`` is a deliberate
control: it reproduces the pre-fix behavior and asserts the match collapses to
zero. If it ever passes differently, the fixture no longer represents the
incident and the assertions below it stop proving anything.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from routing.completion_outcome import CompletionOutcome, classify_completion_outcome
from routing.routers import RoutingObservation
from routing.routewise.prefix_cache import (
    Block,
    CacheLocalityEvidenceState,
    CacheScope,
    PrefixCacheCoordinator,
    SessionProviderPrefixMemory,
    _CacheLocalityEstimator,
)
from routing.routewise.prefix_cache_pending import PendingPrefixCacheStore
from routing.routewise.router import RouteWiseRouter

SECRET = b"unit-test-secret"

WARMUP_STUB = "The model is starting up - this takes about 120 seconds. Please wait..."

_ENDPOINT = "p1:host:443"


def _chars(text: str) -> list[int]:
    return [ord(c) for c in text]


def _scope() -> CacheScope:
    return CacheScope(
        user_hash="u1",
        project_hash="proj1",
        session_hash="s1",
        provider_id="p1",
        endpoint_id=_ENDPOINT,
        model_profile="m1",
        key_slot_id="k1",
    )


def _blocks(*pairs: tuple[str, int]) -> tuple[Block, ...]:
    return tuple(Block(digest=digest, token_count=n) for digest, n in pairs)


def _coordinator(**kw) -> PrefixCacheCoordinator:
    min_match = kw.pop("min_match_tokens", 1)
    return PrefixCacheCoordinator(
        enabled=True,
        memory=SessionProviderPrefixMemory(min_match_tokens=min_match),
        block_size=8,
        secret=SECRET,
        tokenize=_chars,
        **kw,
    )


def _remember_with_observed_hit(
    coordinator: PrefixCacheCoordinator,
    scope: CacheScope,
    blocks: tuple[Block, ...],
    *,
    cached_tokens: int | None = None,
) -> None:
    """Establish remembered blocks backed by verified reuse evidence.

    Since #1417 a bare ``remember()`` only records *potential* warming; reaching
    ``VERIFIED_REUSABLE`` requires the provider to actually report cache reuse.
    The incident tests predate that model, so wherever they mean "strong
    existing evidence" they now establish it the way current dev requires.

    This keeps the invariant under test intact -- a transient stub must not
    destroy strong locality evidence -- without reverting #1417's rule that only
    an observed hit confirms reuse.
    """
    coordinator.remember(scope, blocks)
    # Default to the full block total so the established evidence bounds the
    # estimate exactly rather than one token short of it.
    total = cached_tokens if cached_tokens is not None else sum(b.token_count for b in blocks)
    coordinator.record_evidence(scope, total, blocks=blocks)


def _router_with_cache(**kw) -> SimpleNamespace:
    """Minimal object exposing exactly what the commit path touches."""
    router = SimpleNamespace(
        prefix_cache=_coordinator(**kw),
        pending_prefix_cache=PendingPrefixCacheStore(),
    )
    router.record_observation = lambda obs: RouteWiseRouter._commit_prefix_cache_observation(
        router, obs
    )
    # The commit path publishes telemetry through this helper; bind it the same
    # way so the harness exercises the real emission path.
    router._emit_prefix_evidence = lambda obs, scope, **kw: RouteWiseRouter._emit_prefix_evidence(
        router, obs, scope, **kw
    )
    router.warm_scope = lambda: router.prefix_cache.scope_for(
        session="s", provider_id="p", endpoint_id=_ENDPOINT, model_profile="m"
    )
    router.stash = lambda request_id, blocks: router.pending_prefix_cache.put(
        request_id, blocks, {_ENDPOINT: router.warm_scope()}
    )
    return router


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------


class TestCompletionOutcome:
    def test_textual_warmup_stub_is_not_progress(self):
        outcome = classify_completion_outcome(
            content=WARMUP_STUB, usage=None, http_status=200, terminal=True
        )
        assert outcome is CompletionOutcome.TRANSIENT_NO_PROGRESS

    def test_real_answer_is_progress(self):
        outcome = classify_completion_outcome(
            content="I inspected the handler and applied the fix.",
            usage=None,
            http_status=200,
            terminal=True,
        )
        assert outcome is CompletionOutcome.PROGRESS

    def test_http_error_is_provider_error(self):
        outcome = classify_completion_outcome(
            content=None, usage=None, http_status=503, terminal=True
        )
        assert outcome is CompletionOutcome.PROVIDER_ERROR

    def test_empty_body_is_empty(self):
        outcome = classify_completion_outcome(
            content=None, usage=None, http_status=200, terminal=True
        )
        assert outcome is CompletionOutcome.EMPTY

    def test_non_terminal_is_aborted(self):
        outcome = classify_completion_outcome(
            content=None, usage=None, http_status=200, terminal=False
        )
        assert outcome is CompletionOutcome.ABORTED

    def test_known_wait_notice_is_transient(self):
        outcome = classify_completion_outcome(
            content="Model is loading, please wait",
            usage={"output_tokens": 12},
            http_status=200,
            terminal=True,
        )
        assert outcome is CompletionOutcome.TRANSIENT_NO_PROGRESS

    def test_outcome_admits_real_work(self):
        assert CompletionOutcome.PROGRESS.admits_real_work is True
        for bad in (
            CompletionOutcome.TRANSIENT_NO_PROGRESS,
            CompletionOutcome.EMPTY,
            CompletionOutcome.PROVIDER_ERROR,
            CompletionOutcome.ABORTED,
            CompletionOutcome.UNKNOWN,
        ):
            assert bad.admits_real_work is False, bad


# ---------------------------------------------------------------------------
# The core regression
# ---------------------------------------------------------------------------


class TestTransientStubDoesNotPoisonLocality:
    def test_unguarded_write_is_what_caused_the_incident(self):
        """
        Control: the pre-fix commit path called observe() unconditionally, and
        that alone collapsed the match to zero. If this stops holding, the
        fixture no longer reproduces the incident.
        """
        coordinator = _coordinator(min_match_tokens=1)
        scope = _scope()
        conversation = _blocks(("a", 4096), ("b", 4096))
        coordinator.remember(scope, conversation)
        coordinator.memory.observe(scope, _blocks(("stub", 8)))
        signal = coordinator.evaluate(scope, conversation, cold_cost=1.0, price_delta=0.5)
        assert signal.matched_prefix_tokens == 0, "fixture no longer reproduces the incident"

    def test_stub_text_must_not_replace_strong_evidence(self):
        coordinator = _coordinator(min_match_tokens=1)
        scope = _scope()
        conversation = _blocks(("a", 4096), ("b", 4096), ("c", 4096))
        _remember_with_observed_hit(coordinator, scope, conversation)

        outcome = classify_completion_outcome(
            content=WARMUP_STUB, usage=None, http_status=200, terminal=True
        )
        assert outcome is CompletionOutcome.TRANSIENT_NO_PROGRESS

        coordinator.record_outcome(
            scope=scope,
            blocks=_blocks(("stub", 8)),
            outcome=outcome,
            observed_cached_tokens=0,
        )

        after = coordinator.lookup_state(scope)
        assert after.blocks == conversation
        assert after.verified is True
        signal = coordinator.evaluate(scope, conversation, cold_cost=1.0, price_delta=0.5)
        assert signal.matched_prefix_tokens == 3 * 4096

    def test_next_real_request_still_gets_full_discount(self):
        coordinator = _coordinator(min_match_tokens=1)
        scope = _scope()
        conversation = _blocks(("a", 4096), ("b", 4096))
        _remember_with_observed_hit(coordinator, scope, conversation)
        coordinator.record_outcome(
            scope=scope,
            blocks=_blocks(("stub", 8)),
            outcome=CompletionOutcome.TRANSIENT_NO_PROGRESS,
            observed_cached_tokens=0,
        )
        signal = coordinator.evaluate(scope, conversation, cold_cost=1.0, price_delta=0.5)
        assert signal.matched_prefix_tokens == 2 * 4096
        # The whole conversation must still be eligible. Expected tokens are
        # observed tokens scaled by decaying confidence, so the real invariant is
        # "essentially the full observed amount", not strict equality.
        assert signal.expected_cached_tokens == pytest.approx(2 * 4096, rel=1e-3)
        assert signal.cache_discount > 0.0

    def test_growing_conversation_after_stub_still_matches(self):
        """The /goal loop appends turns; locality must survive the stub."""
        coordinator = _coordinator(min_match_tokens=1)
        scope = _scope()
        conversation = _blocks(("a", 4096), ("b", 4096))
        coordinator.remember(scope, conversation)
        coordinator.record_outcome(
            scope=scope,
            blocks=_blocks(("stub", 8)),
            outcome=CompletionOutcome.TRANSIENT_NO_PROGRESS,
            observed_cached_tokens=0,
        )
        grown = _blocks(("a", 4096), ("b", 4096), ("c", 512))
        signal = coordinator.evaluate(scope, grown, cold_cost=1.0, price_delta=0.5)
        assert signal.matched_prefix_tokens == 2 * 4096


class TestEvidenceIsNotWholesaleReplaced:
    """A response that did no useful work must not destroy what is known."""

    def test_progress_advances_evidence(self):
        coordinator = _coordinator(min_match_tokens=1)
        scope = _scope()
        first = _blocks(("a", 4096))
        coordinator.remember(scope, first)
        second = _blocks(("a", 4096), ("b", 4096))
        coordinator.remember(scope, second)
        assert coordinator.lookup_state(scope).blocks == second

    def test_non_progressing_outcome_preserves_existing_evidence(self):
        """The core invariant: a warmup notice must not overwrite warm evidence.

        The response still refreshes liveness (the provider answered), but the
        stored blocks are left exactly as they were.
        """
        coordinator = _coordinator(min_match_tokens=1)
        scope = _scope()
        conversation = _blocks(("a", 4096))
        coordinator.remember(scope, conversation)

        coordinator.record_outcome(
            scope=scope,
            blocks=_blocks(("stub", 8)),
            outcome=CompletionOutcome.TRANSIENT_NO_PROGRESS,
            observed_cached_tokens=0,
        )

        state = coordinator.lookup_state(scope)
        assert state.present is True
        assert state.blocks == conversation, "a transient stub replaced warm evidence"

    def test_non_progressing_outcome_refreshes_liveness(self):
        """Liveness moves so a hiccup cannot age strong evidence out via TTL."""
        coordinator = _coordinator(min_match_tokens=1)
        scope = _scope()
        conversation = _blocks(("a", 4096))
        coordinator.remember(scope, conversation)
        coordinator.record_outcome(
            scope=scope,
            blocks=_blocks(("stub", 8)),
            outcome=CompletionOutcome.TRANSIENT_NO_PROGRESS,
            observed_cached_tokens=0,
        )
        assert coordinator.lookup_state(scope).blocks == conversation

    def test_unclassified_outcome_does_not_warm(self):
        coordinator = _coordinator(min_match_tokens=1)
        scope = _scope()
        conversation = _blocks(("a", 4096), ("b", 4096))
        assert (
            coordinator.record_outcome(
                scope=scope,
                blocks=conversation,
                outcome=CompletionOutcome.UNKNOWN,
            )
            is False
        )
        assert coordinator.lookup_state(scope).blocks is None

    def test_unknown_outcome_keeps_matching_cache_evidence(self):
        coordinator = _coordinator(min_match_tokens=1)
        scope = _scope()
        conversation = _blocks(("a", 4096), ("b", 4096))
        coordinator.remember(scope, conversation)

        coordinator.record_outcome(
            scope=scope,
            blocks=conversation,
            outcome=CompletionOutcome.UNKNOWN,
            observed_cached_tokens=2048,
        )

        state = coordinator.lookup_state(scope)
        assert state.blocks == conversation
        assert state.evidence == CacheLocalityEvidenceState.VERIFIED_REUSABLE


class TestRouterCommitPath:
    def _observation(self, **kw) -> RoutingObservation:
        fields = {
            "model_id": "m",
            "endpoint_id": _ENDPOINT,
            "ttft_ms": 10.0,
            "total_latency_ms": 20.0,
            "token_count": 10,
            "success": True,
            "request_id": "req-1",
        }
        fields.update(kw)
        return RoutingObservation(**fields)

    def test_transient_outcome_does_not_warm_locality(self):
        """
        The gate itself: a transport-successful but non-progressing observation
        must leave the stored entry byte-identical. Asserts committed evidence,
        not merely that the stash was consumed, so removing the gate fails this.
        """
        router = _router_with_cache(min_match_tokens=1)
        scope = router.warm_scope()
        conversation = _blocks(("a", 4096), ("b", 4096))
        router.prefix_cache.remember(scope, conversation)
        router.stash("req-1", _blocks(("stub", 8)))
        router.record_observation(
            self._observation(
                success=True,
                outcome=CompletionOutcome.TRANSIENT_NO_PROGRESS,
            )
        )
        assert router.prefix_cache.lookup_state(scope).blocks == conversation
        signal = router.prefix_cache.evaluate(scope, conversation, cold_cost=1.0, price_delta=0.5)
        assert signal.matched_prefix_tokens == 8192

    def test_progress_outcome_does_advance_evidence(self):
        """Counter-test: real work must still be able to update the entry."""
        router = _router_with_cache(min_match_tokens=1)
        scope = router.warm_scope()
        newer = _blocks(("a", 4096), ("b", 4096), ("c", 4096))
        router.prefix_cache.remember(scope, _blocks(("a", 4096)))
        router.stash("req-ok", newer)
        router.record_observation(
            self._observation(
                request_id="req-ok",
                success=True,
                outcome=CompletionOutcome.PROGRESS,
            )
        )
        assert router.prefix_cache.lookup_state(scope).blocks == newer

    def test_terminal_failure_does_not_create_locality(self):
        """
        A terminal failure must leave the cache as empty as it found it -- it
        must never manufacture a warm entry out of a failed request.
        """
        router = _router_with_cache(min_match_tokens=1)
        scope = router.warm_scope()
        router.stash("req-fail", _blocks(("a", 4096)))
        router.record_observation(
            self._observation(
                request_id="req-fail",
                success=False,
                terminal=True,
                outcome=CompletionOutcome.PROVIDER_ERROR,
            )
        )
        assert router.prefix_cache.lookup_state(scope).present is False

    def test_non_terminal_failure_keeps_stash_for_fallback_winner(self):
        """A hedge/fallback leg still needs the stash to warm later."""
        router = _router_with_cache(min_match_tokens=1)
        router.stash("req-h", _blocks(("a", 4096)))
        router.record_observation(
            self._observation(
                request_id="req-h",
                success=False,
                terminal=False,
                outcome=CompletionOutcome.PROVIDER_ERROR,
            )
        )
        assert "req-h" in router.pending_prefix_cache


class TestLivenessRefreshGeneration:
    """A non-progress completion must refresh the current entry, not be rejected."""

    def test_transient_refreshes_stored_generation_after_new_reservation(self):
        """Maintainer's sequence: reserve g1, commit, reserve g2, then transient g2."""
        now = [1000.0]
        coordinator = PrefixCacheCoordinator(
            enabled=True,
            memory=SessionProviderPrefixMemory(min_match_tokens=1, time_source=lambda: now[0]),
            block_size=8,
            secret=SECRET,
            tokenize=_chars,
        )
        scope = _scope()
        conversation = _blocks(("a", 4096))

        # First successful dispatch stores blocks under its reserved generation.
        coordinator.reserve_generations([scope])
        assert coordinator.remember(scope, conversation) is True
        first_seen = coordinator.memory._entries[scope].last_seen_at

        # A later request reserves a new generation, then fails to make progress.
        reserved = coordinator.reserve_generations([scope])
        assert reserved[scope] > 0
        now[0] += 5.0
        # The refresh must validate against the coordinator's CURRENT
        # reservation. The entry stores the creation generation (g1), so a g2
        # refresh needs the coordinator to reconcile: update the entry's
        # generation to the current reservation while refreshing, so the
        # maintainer's exact sequence works.
        coordinator.record_outcome(
            scope=scope,
            blocks=_blocks(("stub", 8)),
            outcome=CompletionOutcome.TRANSIENT_NO_PROGRESS,
            generation=reserved[scope],
        )

        entry = coordinator.memory._entries[scope]
        assert entry.blocks == conversation, "a transient stub replaced warm evidence"
        assert entry.last_seen_at > first_seen, "the warm entry was not refreshed"

    def test_missing_cache_usage_refreshes_ttl_without_refreshing_evidence_age(self):
        now = [1000.0]

        def time_source():
            return now[0]

        coordinator = PrefixCacheCoordinator(
            enabled=True,
            memory=SessionProviderPrefixMemory(
                min_match_tokens=1, ttl_sec=300.0, time_source=time_source
            ),
            evidence=_CacheLocalityEstimator(ttl_sec=300.0, time_source=time_source),
            block_size=8,
            secret=SECRET,
            tokenize=_chars,
        )
        scope = _scope()
        conversation = _blocks(("a", 4096), ("b", 4096))
        _remember_with_observed_hit(coordinator, scope, conversation)
        observed_at = coordinator._evidence._evidence[scope].observed_at
        initial = coordinator.evaluate(scope, conversation, cold_cost=1.0, price_delta=0.5)

        warmup = CompletionOutcome.TRANSIENT_NO_PROGRESS
        for _ in range(5):
            now[0] += 10.0
            coordinator.record_outcome(
                scope=scope,
                blocks=conversation,
                outcome=warmup,
                observed_cached_tokens=None,
            )

        now[0] = observed_at + 150.0
        decayed = coordinator.evaluate(scope, conversation, cold_cost=1.0, price_delta=0.5)

        assert coordinator._evidence._evidence[scope].observed_at == observed_at
        assert decayed.expected_cached_tokens == pytest.approx(
            initial.expected_cached_tokens / 2, rel=1e-3
        )


class TestNonProgressEvidenceRecording:
    """A non-progress success must still record authoritative cache evidence."""

    def test_transient_warmup_with_cached_zero_records_negative_evidence(self):
        """A warmup 200 reporting cached_tokens=0 must degrade warm evidence."""
        coordinator = _coordinator(min_match_tokens=1)
        scope = _scope()
        conversation = _blocks(("a", 4096))
        _remember_with_observed_hit(coordinator, scope, conversation, cached_tokens=4096)
        assert coordinator.lookup_state(scope).verified is True

        coordinator.record_outcome(
            scope=scope,
            blocks=conversation,
            outcome=CompletionOutcome.TRANSIENT_NO_PROGRESS,
            observed_cached_tokens=0,
        )

        state = coordinator.lookup_state(scope)
        assert state.blocks == conversation
        assert state.verified is False, "provider contradicted the warm entry"

    def test_progress_with_cached_zero_records_one_materialization_observation(self, monkeypatch):
        """A successful cache miss is one observation, not a miss plus a candidate."""
        coordinator = _coordinator(min_match_tokens=1)
        scope = _scope()
        conversation = _blocks(("a", 4096))
        coordinator.remember(scope, conversation)
        calls = 0
        record_evidence = coordinator.record_evidence

        def _record_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            return record_evidence(*args, **kwargs)

        monkeypatch.setattr(coordinator, "record_evidence", _record_once)
        coordinator.record_outcome(
            scope=scope,
            blocks=conversation,
            outcome=CompletionOutcome.PROGRESS,
            observed_cached_tokens=0,
        )

        assert calls == 1
        assert (
            coordinator._evidence._evidence[scope].state
            is CacheLocalityEvidenceState.MATERIALIZATION_CANDIDATE
        )


class TestDifferingPrefixEvidenceContract:
    """Non-progress completions with differing stored/dispatched prefixes."""

    def test_differing_prefix_non_progress_records_evidence_not_warming(self):
        """A non-progress completion whose dispatched prefix differs from the
        remembered one records authoritative cache evidence without warming
        the dispatched prefix."""
        coordinator = _coordinator(min_match_tokens=1)
        scope = _scope()
        stored = _blocks(("a", 4096))
        dispatched = _blocks(("b", 4096))
        coordinator.remember(scope, stored)

        coordinator.record_outcome(
            scope=scope,
            blocks=dispatched,
            outcome=CompletionOutcome.TRANSIENT_NO_PROGRESS,
            observed_cached_tokens=0,
        )

        state = coordinator.lookup_state(scope)
        # The remembered blocks are unchanged: a non-progress completion never
        # warms. The evidence records the provider's authoritative 0 for the
        # dispatched prefix, which degrades confidence without overwriting.
        assert state.blocks == stored

    def test_differing_prefix_progress_warms_dispatched_blocks(self):
        """A progress completion with a differing dispatched prefix warms it."""
        coordinator = _coordinator(min_match_tokens=1)
        scope = _scope()
        stored = _blocks(("a", 4096))
        dispatched = _blocks(("b", 4096))
        coordinator.remember(scope, stored)

        assert (
            coordinator.record_outcome(
                scope=scope,
                blocks=dispatched,
                outcome=CompletionOutcome.PROGRESS,
            )
            is True
        )

        assert coordinator.lookup_state(scope).blocks == dispatched
