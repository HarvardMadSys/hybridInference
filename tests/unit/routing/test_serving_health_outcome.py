"""Serving health must require evidence of useful model progress.

Incident shape
--------------
A provider answered HTTP 200 with a well-formed body whose only content was a
warmup notice::

    "The model is starting up - this takes about 120 seconds. Please wait..."

That response *is* transport success: the socket was fine, the process was
alive, readiness passed. Every existing success path therefore counted it as a
healthy serving result, and ``record_success`` clears ``consecutive_failures``,
closes an OPEN or HALF_OPEN breaker, and ends a credential-rejection run.

During a cold start or model-loading stall that response repeats. Each repeat
re-closed a breaker the previous failures had just opened, so the endpoint read
as healthy while producing no work at all -- the process was up, the circuit was
closed, and no request was actually being served.

The durable rule lives in ``EndpointHealthRegistry.record_success`` rather than
at its many call sites, so these tests drive the registry directly. They also
pin the liveness/progress separation: a healthy *process* is not evidence of
useful *serving*.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from routing.completion_outcome import CompletionOutcome
from routing.endpoint_health import EndpointHealthRegistry, _CircuitState

_ENDPOINT = "openai:api.example.com:443"
_THRESHOLD_ENV = "CIRCUIT_FAILURE_THRESHOLD"

WARMUP_STUB = "The model is starting up - this takes about 120 seconds. Please wait..."
REAL_ANSWER = "Applied the fix and verified it."


def _body(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


# Outcomes a provider can report that are NOT evidence of useful work.
NO_PROGRESS_OUTCOMES = [
    CompletionOutcome.TRANSIENT_NO_PROGRESS,
    CompletionOutcome.EMPTY,
    CompletionOutcome.PROVIDER_ERROR,
    CompletionOutcome.ABORTED,
    CompletionOutcome.UNKNOWN,
]


class _StatusError(Exception):
    """Adapter exception carrying an upstream HTTP status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def _trip(monkeypatch, registry: EndpointHealthRegistry, endpoint_id: str = _ENDPOINT) -> None:
    """Drive ``endpoint_id`` into an OPEN breaker via consecutive failures."""
    for _ in range(3):
        registry.record_failure(endpoint_id, reason="upstream_502")
    assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.OPEN


def test_repeated_warmup_responses_cannot_reset_serving_health(monkeypatch):
    """The incident itself: many non-empty warmup responses, breaker stays open."""
    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    registry = EndpointHealthRegistry()
    _trip(monkeypatch, registry)

    for _ in range(5):
        registry.record_success(_ENDPOINT, outcome=CompletionOutcome.TRANSIENT_NO_PROGRESS)

    circuit = registry._circuits[_ENDPOINT]
    assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.OPEN
    assert circuit.consecutive_failures == 3


def test_genuine_completion_recovers_the_breaker_normally(monkeypatch):
    """The other half of the contract: real work must still recover the breaker."""
    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    registry = EndpointHealthRegistry()
    _trip(monkeypatch, registry)

    registry.record_success(_ENDPOINT, outcome=CompletionOutcome.PROGRESS)

    circuit = registry._circuits[_ENDPOINT]
    assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.CLOSED
    assert circuit.consecutive_failures == 0


def test_every_non_progress_outcome_is_inert(monkeypatch):
    """No outcome lacking real work may clear accumulated failure state."""
    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    for outcome in NO_PROGRESS_OUTCOMES:
        registry = EndpointHealthRegistry()
        _trip(monkeypatch, registry)
        registry.record_success(_ENDPOINT, outcome=outcome)
        assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.OPEN, outcome
        assert registry._circuits[_ENDPOINT].consecutive_failures == 3, outcome


def test_non_progress_does_not_force_close_half_open(monkeypatch):
    """A half-open breaker must stay half-open until real work proves recovery."""
    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "0")
    registry = EndpointHealthRegistry()
    _trip(monkeypatch, registry)

    assert registry.begin_dispatch(_ENDPOINT) is not None
    assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.HALF_OPEN

    for outcome in NO_PROGRESS_OUTCOMES:
        registry.record_success(_ENDPOINT, outcome=outcome)
        assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.HALF_OPEN, outcome

    registry.record_success(_ENDPOINT, outcome=CompletionOutcome.PROGRESS)
    assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.CLOSED


def test_liveness_never_recovers_a_half_open_breaker(monkeypatch):
    """Transport liveness cannot close a breaker; only serving progress can."""
    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "0")
    registry = EndpointHealthRegistry()
    _trip(monkeypatch, registry)

    assert registry.begin_dispatch(_ENDPOINT) is not None
    assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.HALF_OPEN

    for _ in range(5):
        registry.record_liveness(_ENDPOINT)
        assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.HALF_OPEN

    registry.record_success(_ENDPOINT, outcome=CompletionOutcome.PROGRESS)
    assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.CLOSED


def test_liveness_does_not_clear_credential_rejections(monkeypatch):
    """Liveness proves the endpoint emitted bytes, not that auth recovered."""
    monkeypatch.setenv(_THRESHOLD_ENV, "999")
    registry = EndpointHealthRegistry()

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()):
        registry.record_failure(_ENDPOINT, reason="stream_exception", exc=_StatusError(401))
        assert registry.snapshot()[_ENDPOINT]["consecutive_auth_rejections"] == 1

        registry.record_liveness(_ENDPOINT)
        assert registry.snapshot()[_ENDPOINT]["consecutive_auth_rejections"] == 1

        registry.record_success(_ENDPOINT, outcome=CompletionOutcome.PROGRESS)
        assert registry.snapshot()[_ENDPOINT]["consecutive_auth_rejections"] == 0


def test_unclassified_success_is_rejected_not_counted(monkeypatch):
    """Silence is never positive evidence; callers must use liveness instead."""
    registry = EndpointHealthRegistry()
    _trip(monkeypatch, registry)

    with pytest.raises(TypeError):
        registry.record_success(_ENDPOINT)

    assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.OPEN


def test_process_liveness_is_not_serving_progress(monkeypatch):
    """An endpoint that answers but never infers must not claim serving progress.

    ``ensure`` and the snapshot are the process-level liveness surface: they stay
    healthy because the process really is alive. Only the breaker and the failure
    count -- the serving-health surface -- carry the adverse verdict.
    """
    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    registry = EndpointHealthRegistry()
    registry.ensure(_ENDPOINT)

    for _ in range(4):
        registry.record_success(_ENDPOINT, outcome=CompletionOutcome.EMPTY)

    full = registry.snapshot()
    # Liveness: the endpoint is tracked and reachable.
    assert _ENDPOINT in full
    assert full[_ENDPOINT]["circuit_state"] == _CircuitState.CLOSED
    # Serving progress: nothing was produced, so no failure is invented either.
    assert registry._circuits[_ENDPOINT].consecutive_failures == 0


def test_serving_health_correction_does_not_disturb_locality_evidence():
    """Serving-health updates leave the remembered prefix history untouched."""
    from routing.completion_outcome import CompletionOutcome
    from routing.routewise.prefix_cache import (
        Block,
        CacheScope,
        PrefixCacheCoordinator,
        SessionProviderPrefixMemory,
    )

    coordinator = PrefixCacheCoordinator(
        enabled=True,
        memory=SessionProviderPrefixMemory(min_match_tokens=1),
        block_size=8,
        secret=b"unit-test-secret",
        tokenize=lambda t: [ord(c) for c in t],
    )
    scope = CacheScope(
        user_hash="u",
        project_hash="p",
        session_hash="s",
        provider_id="prov",
        endpoint_id=_ENDPOINT,
        model_profile="m",
        key_slot_id="k",
        cache_affecting_params_hash="",
    )
    conversation = (Block(digest="a", token_count=4096),)
    coordinator.remember(scope, conversation)
    before = coordinator.memory.lookup(scope, conversation).matched_prefix_tokens
    registry = EndpointHealthRegistry()
    registry.record_failure(_ENDPOINT, reason="upstream_502")
    registry.record_success(_ENDPOINT, outcome=CompletionOutcome.TRANSIENT_NO_PROGRESS)
    after = coordinator.memory.lookup(scope, conversation).matched_prefix_tokens

    assert before == 4096
    assert after == before


# ---------------------------------------------------------------------------
# Router-level integration
#
# The registry tests above pin the authority rule directly. These prove both
# routers route through that authority, so the rule holds for real traffic and
# not merely for callers that pass an outcome by hand.
# ---------------------------------------------------------------------------


def _routers():
    from routing.routers import FixedRouter
    from routing.routewise.config import RouteWiseConfig
    from routing.routewise.router import RouteWiseRouter

    return (
        FixedRouter(health_registry=EndpointHealthRegistry()),
        RouteWiseRouter(config=RouteWiseConfig(), health_registry=EndpointHealthRegistry()),
    )


def test_both_routers_route_serving_health_through_the_outcome_gate(monkeypatch):
    """FixedRouter and RouteWise must behave identically on a warmup response."""
    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    for router in _routers():
        name = type(router).__name__
        registry = router._health_registry
        _trip(monkeypatch, registry)

        router._on_success(_ENDPOINT, _body(WARMUP_STUB))

        assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.OPEN, name
        assert registry._circuits[_ENDPOINT].consecutive_failures == 3, name


def test_both_routers_recover_on_genuine_progress(monkeypatch):
    """Real work must still close the breaker on both routers."""
    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    for router in _routers():
        name = type(router).__name__
        registry = router._health_registry
        _trip(monkeypatch, registry)

        router._on_success(_ENDPOINT, _body(REAL_ANSWER))

        assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.CLOSED, name


# ---------------------------------------------------------------------------
# Streaming success must close breakers
# ---------------------------------------------------------------------------


async def test_streaming_first_content_closes_open_breaker():
    """A successful stream resets consecutive failures and closes the breaker."""
    from routing.routers import FixedRouter

    registry = EndpointHealthRegistry()
    import pytest as _pytest

    _trip(_pytest.MonkeyPatch(), registry)

    class _StreamAdapter:
        """Emit one content-bearing SSE chunk."""

        class config:
            provider = "stream-provider"
            base_url = "https://stream.example/v1"
            endpoint_id = _ENDPOINT

        async def stream_chat_completion(self, messages, **params):
            yield 'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'

    router = FixedRouter.__new__(FixedRouter)
    router._health_registry = registry

    chunks = ['data: {"choices":[{"delta":{"content":"hello"}}]}\n\n']
    router._on_stream_first_content(_ENDPOINT, chunks)

    assert registry._circuits[_ENDPOINT].consecutive_failures == 0
    assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.CLOSED


async def test_fragmented_warmup_stream_does_not_close_breaker():
    """A warmup notice split across chunks must not record semantic success."""
    from routing.routers import FixedRouter

    registry = EndpointHealthRegistry()
    import pytest as _pytest

    _trip(_pytest.MonkeyPatch(), registry)

    router = FixedRouter.__new__(FixedRouter)
    router._health_registry = registry

    # The same warmup notice as WARMUP_NOTICE above, split across three SSE chunks.
    chunks = [
        'data: {"choices":[{"delta":{"content":"The model"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":" is starting up - this takes"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":" about 120 seconds. Please wait..."}}]}\n\n',
    ]
    router._on_stream_first_content(_ENDPOINT, chunks)

    assert registry._circuits[_ENDPOINT].consecutive_failures == 3
    assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.OPEN


async def test_fragmented_genuine_stream_closes_breaker():
    """A genuine answer split across chunks must still close the breaker."""
    from routing.routers import FixedRouter

    registry = EndpointHealthRegistry()
    import pytest as _pytest

    _trip(_pytest.MonkeyPatch(), registry)

    router = FixedRouter.__new__(FixedRouter)
    router._health_registry = registry

    chunks = [
        'data: {"choices":[{"delta":{"content":"Applied the fix"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":" and verified it."}}]}\n\n',
    ]
    router._on_stream_first_content(_ENDPOINT, chunks)

    assert registry._circuits[_ENDPOINT].consecutive_failures == 0
    assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.CLOSED


@pytest.mark.parametrize(
    "delta",
    [
        {"tool_calls": [{"id": "call-1", "function": {"name": "lookup"}}]},
        {"thinking": "reasoning before visible text"},
        {"reasoning_content": "reasoning before visible text"},
    ],
)
def test_fixed_and_routewise_health_recorders_accept_structured_stream_output(monkeypatch, delta):
    """Structured first deltas are real work for both router health owners."""
    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    chunk = f'data: {{"choices":[{{"delta":{json.dumps(delta)}}}]}}\n\n'

    for router in _routers():
        registry = router._health_registry
        _trip(monkeypatch, registry)

        router._on_stream_first_content(_ENDPOINT, [chunk])

        assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.CLOSED
        assert registry._circuits[_ENDPOINT].consecutive_failures == 0


def test_fixed_and_routewise_health_recorders_defer_partial_warmup_prefix(monkeypatch):
    """A stream fragment that could become a warmup notice is not success."""
    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    first_chunk = 'data: {"choices":[{"delta":{"content":"The"}}]}\n\n'

    for router in _routers():
        registry = router._health_registry
        _trip(monkeypatch, registry)

        assert router._on_stream_first_content(_ENDPOINT, [first_chunk]) is None
        assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.OPEN
        assert registry._circuits[_ENDPOINT].consecutive_failures == 3


class _StreamingAdapter:
    """Minimal endpoint adapter used to exercise both public stream paths."""

    def __init__(
        self,
        chunks: list[str],
        *,
        endpoint_id: str = _ENDPOINT,
        delay_sec: float = 0.0,
    ) -> None:
        self.config = SimpleNamespace(
            id="stream-model",
            name="stream-model",
            provider="stream-provider",
            base_url="https://stream.example/v1",
            endpoint_id=endpoint_id,
            context_length=8192,
            input_modalities=["text"],
            output_modalities=["text"],
            supports_tools=False,
        )
        self.chunks = chunks
        self.delay_sec = delay_sec

    async def stream_chat_completion(self, messages, **params):
        for chunk in self.chunks:
            if self.delay_sec:
                await asyncio.sleep(self.delay_sec)
            yield chunk


def _stream_chunks(contents: list[str]) -> list[str]:
    return [
        f'data: {{"choices":[{{"delta":{{"content":{json.dumps(content)}}}}}]}}\n\n'
        for content in contents
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("router_kind", ["fixed", "routewise"])
@pytest.mark.parametrize(
    ("contents", "expected_failure_counts", "expected_outcome"),
    [
        (
            ["The", " model is starting up - this takes about 120 seconds. Please wait..."],
            [2, 2],
            CompletionOutcome.TRANSIENT_NO_PROGRESS,
        ),
        (["The", " answer is 42"], [2, 0], CompletionOutcome.PROGRESS),
    ],
    ids=["warmup-prefix", "substantive-continuation"],
)
async def test_router_stream_entrypoints_defer_health_until_prefix_resolves(
    monkeypatch,
    router_kind,
    contents,
    expected_failure_counts,
    expected_outcome,
):
    """Only the resolved semantic prefix may reset serving failures."""
    from routing.completion_outcome import classify_stream_prefix
    from routing.routers import FixedRouter
    from routing.routewise.config import RouteWiseConfig
    from routing.routewise.decisions import ProviderReservation, RoutingDecision, RoutingTrace
    from routing.routewise.router import RouteWiseRouter

    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    registry = EndpointHealthRegistry()
    registry.record_failure(_ENDPOINT, reason="upstream_502")
    registry.record_failure(_ENDPOINT, reason="upstream_502")
    adapter = _StreamingAdapter(_stream_chunks(contents))
    assert classify_stream_prefix("".join(contents), terminal=True) is expected_outcome

    if router_kind == "fixed":
        router = FixedRouter(health_registry=registry)
        router.register_route("stream-model", [(adapter, 1.0)])
    else:
        router = RouteWiseRouter(
            config=RouteWiseConfig(db_bootstrap_enabled=False),
            health_registry=registry,
        )
        decision = RoutingDecision(
            adapter=adapter,
            reservation=ProviderReservation(),
            metadata={},
            trace=RoutingTrace(request_id="stream-health-test"),
        )
        router._select_decision = lambda *args, **kwargs: decision

    observed_failure_counts = []
    async for chunk in router.stream_chat_completion(
        "stream-model",
        [{"role": "user", "content": "question"}],
        request_id="stream-health-test",
    ):
        if '"content"' in chunk and '"_routing"' not in chunk:
            observed_failure_counts.append(registry._circuits[_ENDPOINT].consecutive_failures)

    assert observed_failure_counts == expected_failure_counts
    if expected_failure_counts[-1] == 2:
        assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.CLOSED
    else:
        assert registry._circuits[_ENDPOINT].consecutive_failures == 0


@pytest.mark.asyncio
async def test_routewise_partial_warmup_prefix_does_not_record_success_on_error(monkeypatch):
    """A provider error after an undecided prefix records failure, never success."""
    from routing.completion_outcome import classify_stream_prefix
    from routing.routewise.config import RouteWiseConfig
    from routing.routewise.decisions import ProviderReservation, RoutingDecision, RoutingTrace
    from routing.routewise.router import RouteWiseRouter

    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    registry = EndpointHealthRegistry()
    registry.record_failure(_ENDPOINT, reason="upstream_502")
    registry.record_failure(_ENDPOINT, reason="upstream_502")

    class _FailingAfterPrefix(_StreamingAdapter):
        async def stream_chat_completion(self, messages, **params):
            yield _stream_chunks(["The"])[0]
            raise ConnectionError("stream interrupted after a possible warmup prefix")

    adapter = _FailingAfterPrefix([])
    router = RouteWiseRouter(
        config=RouteWiseConfig(db_bootstrap_enabled=False),
        health_registry=registry,
    )
    decision = RoutingDecision(
        adapter=adapter,
        reservation=ProviderReservation(),
        metadata={},
        trace=RoutingTrace(request_id="routewise-prefix-error-test"),
    )
    router._select_decision = lambda *args, **kwargs: decision

    assert classify_stream_prefix("The") is None
    with pytest.raises(ConnectionError):
        async for _ in router.stream_chat_completion(
            "stream-model",
            [{"role": "user", "content": "question"}],
            request_id="routewise-prefix-error-test",
        ):
            pass

    assert registry._circuits[_ENDPOINT].consecutive_failures == 3
    assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.OPEN


@pytest.mark.asyncio
async def test_routewise_cancelled_partial_warmup_prefix_does_not_record_success(monkeypatch):
    """Client cancellation leaves a partial possible-warmup outcome undecided."""
    from routing.completion_outcome import classify_stream_prefix
    from routing.routewise.config import RouteWiseConfig
    from routing.routewise.decisions import ProviderReservation, RoutingDecision, RoutingTrace
    from routing.routewise.router import RouteWiseRouter

    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    registry = EndpointHealthRegistry()
    registry.record_failure(_ENDPOINT, reason="upstream_502")
    registry.record_failure(_ENDPOINT, reason="upstream_502")
    waiting = asyncio.Event()

    class _WaitingAfterPrefix(_StreamingAdapter):
        async def stream_chat_completion(self, messages, **params):
            yield _stream_chunks(["The"])[0]
            await waiting.wait()

    adapter = _WaitingAfterPrefix([])
    router = RouteWiseRouter(
        config=RouteWiseConfig(db_bootstrap_enabled=False),
        health_registry=registry,
    )
    decision = RoutingDecision(
        adapter=adapter,
        reservation=ProviderReservation(),
        metadata={},
        trace=RoutingTrace(request_id="routewise-prefix-cancel-test"),
    )
    router._select_decision = lambda *args, **kwargs: decision

    stream = router.stream_chat_completion(
        "stream-model",
        [{"role": "user", "content": "question"}],
        request_id="routewise-prefix-cancel-test",
    )
    while True:
        chunk = await stream.__anext__()
        if '"content"' in chunk:
            break
    assert classify_stream_prefix("The") is None
    await stream.aclose()

    assert registry._circuits[_ENDPOINT].consecutive_failures == 2
    assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.CLOSED


@pytest.mark.asyncio
async def test_hedged_routewise_stream_keeps_fragmented_warmup_non_progress(monkeypatch):
    """The hedge race and RouteWise wrapper both preserve a split warmup outcome."""
    from llm_routewise.core import CheckpointBackupDispatch

    from routing.completion_outcome import classify_stream_prefix
    from routing.response_text import accumulate_stream_prefix
    from routing.routewise.config import RouteWiseConfig
    from routing.routewise.decisions import ProviderReservation, RoutingDecision, RoutingTrace
    from routing.routewise.hedging import HedgedAdapter
    from routing.routewise.router import RouteWiseRouter

    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    primary_endpoint = "primary:stream.example:443"
    backup_endpoint = "backup:stream.example:443"
    warmup_chunks = _stream_chunks(
        [
            "The",
            " model is starting up - this takes",
            " about 120 seconds. Please wait...",
        ]
    )
    registry = EndpointHealthRegistry()
    for endpoint in (primary_endpoint, backup_endpoint):
        for _ in range(3):
            registry.record_failure(endpoint, reason="upstream_502")

    primary = _StreamingAdapter(warmup_chunks, endpoint_id=primary_endpoint)
    backup = _StreamingAdapter(warmup_chunks, endpoint_id=backup_endpoint)
    hedged = HedgedAdapter(
        primary=primary,
        event_sink=registry,
        hedge_checkpoints_sec=(0.0,),
        checkpoint_backup_selector=lambda elapsed, _ts: CheckpointBackupDispatch(
            backup=backup,
            elapsed_sec=elapsed,
            release=lambda: None,
        ),
    )
    router = RouteWiseRouter(
        config=RouteWiseConfig(db_bootstrap_enabled=False),
        health_registry=registry,
    )
    decision = RoutingDecision(
        adapter=hedged,
        reservation=ProviderReservation(),
        metadata={},
        trace=RoutingTrace(request_id="hedged-warmup-stream-test"),
    )
    router._select_decision = lambda *args, **kwargs: decision

    output_chunks = [
        chunk
        async for chunk in router.stream_chat_completion(
            "stream-model",
            [{"role": "user", "content": "question"}],
            request_id="hedged-warmup-stream-test",
        )
    ]

    assert (
        classify_stream_prefix(accumulate_stream_prefix(output_chunks), terminal=True)
        is CompletionOutcome.TRANSIENT_NO_PROGRESS
    )
    for endpoint in (primary_endpoint, backup_endpoint):
        assert registry.snapshot()[endpoint]["circuit_state"] == _CircuitState.OPEN
        assert registry._circuits[endpoint].consecutive_failures == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("router_kind", ["fixed", "routewise", "routewise_hedged"])
@pytest.mark.parametrize(
    "delta",
    [
        {"tool_calls": [{"id": "call-1", "function": {"name": "lookup"}}]},
        {"function_call": {"name": "lookup", "arguments": "{}"}},
        {"reasoning_content": "thinking before visible text"},
        {"thinking": "thinking before visible text"},
    ],
    ids=["tool-calls", "legacy-function-call", "reasoning-content", "thinking"],
)
async def test_router_stream_entrypoints_record_structured_progress(
    monkeypatch, router_kind, delta
):
    """Structured first deltas reach the real health owner on every stream path."""
    from routing.completion_outcome import classify_completion_outcome
    from routing.response_text import chunk_text
    from routing.routers import FixedRouter
    from routing.routewise.config import RouteWiseConfig
    from routing.routewise.decisions import ProviderReservation, RoutingDecision, RoutingTrace
    from routing.routewise.router import RouteWiseRouter

    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    primary_endpoint = "primary:stream.example:443"
    backup_endpoint = "backup:stream.example:443"
    chunk = f"data: {json.dumps({'choices': [{'delta': delta}]})}\n\n"
    registry = EndpointHealthRegistry()
    serving_endpoint = _ENDPOINT
    for endpoint in (_ENDPOINT,):
        for _ in range(2):
            registry.record_failure(endpoint, reason="upstream_502")

    adapter = _StreamingAdapter([chunk])
    if router_kind == "fixed":
        router = FixedRouter(health_registry=registry)
        router.register_route("stream-model", [(adapter, 1.0)])
    else:
        decision_adapter = adapter
        if router_kind == "routewise_hedged":
            from llm_routewise.core import CheckpointBackupDispatch

            from routing.routewise.hedging import HedgedAdapter

            serving_endpoint = backup_endpoint
            for endpoint in (primary_endpoint, backup_endpoint):
                for _ in range(2):
                    registry.record_failure(endpoint, reason="upstream_502")
            backup = _StreamingAdapter([chunk], endpoint_id=backup_endpoint)
            decision_adapter = HedgedAdapter(
                primary=_StreamingAdapter(
                    _stream_chunks(["delayed primary"]),
                    endpoint_id=primary_endpoint,
                    delay_sec=0.05,
                ),
                event_sink=registry,
                hedge_checkpoints_sec=(0.0,),
                checkpoint_backup_selector=lambda elapsed, _ts: CheckpointBackupDispatch(
                    backup=backup,
                    elapsed_sec=elapsed,
                    release=lambda: None,
                ),
            )

        router = RouteWiseRouter(
            config=RouteWiseConfig(db_bootstrap_enabled=False),
            health_registry=registry,
        )
        decision = RoutingDecision(
            adapter=decision_adapter,
            reservation=ProviderReservation(),
            metadata={},
            trace=RoutingTrace(request_id="structured-stream-health-test"),
        )
        router._select_decision = lambda *args, **kwargs: decision

    outcome = classify_completion_outcome(content=chunk_text(chunk), http_status=200, terminal=True)
    assert outcome is CompletionOutcome.PROGRESS

    async for _ in router.stream_chat_completion(
        "stream-model",
        [{"role": "user", "content": "question"}],
        request_id="structured-stream-health-test",
    ):
        pass

    assert registry.snapshot()[serving_endpoint]["circuit_state"] == _CircuitState.CLOSED
    assert registry._circuits[serving_endpoint].consecutive_failures == 0
    if router_kind == "routewise_hedged":
        assert registry.snapshot()[primary_endpoint]["circuit_state"] == _CircuitState.CLOSED
        assert registry._circuits[primary_endpoint].consecutive_failures == 2
