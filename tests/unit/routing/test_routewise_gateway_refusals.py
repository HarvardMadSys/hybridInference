"""A gateway-side refusal is re-solved, and RouteWise learns nothing from it.

``UpstreamSaturated`` (no outbound slot), ``KeyPoolExhausted`` and
``KeyPoolRoleRestricted`` (no key the caller may spend) are raised before
anything is sent, and carry no status on purpose. RouteWise's retry rule read
them by status alone, so it never re-solved: the client got a 500 or 429 while
other candidates sat idle. Each one was also recorded as a 60 s latency
penalty for an endpoint the request never reached.
"""

from __future__ import annotations

import functools
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from routing.routers import RoutingObservation
from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import _is_routewise_retryable_error
from routing.telemetry import failed_attempt, is_uncharged_failure
from serving.adapters.key_pool import KeyPoolExhausted, KeyPoolRoleRestricted
from serving.adapters.upstream_limiter import UpstreamQueueWaitExpired, UpstreamSaturated
from serving.servers.routers.completions_logging import CompletionsLogger
from serving.servers.routers.routing_info import RoutingInfo
from tests.unit.routing.test_routewise_router import _make_router_with_two_api
from tests.unit.servers.test_completions_stream import _make_session

A = "test-model:api-a"
B = "test-model:api-b"

REFUSALS = [
    pytest.param(lambda: UpstreamSaturated("no slot within 30s"), id="saturated"),
    pytest.param(lambda: UpstreamQueueWaitExpired("queue wait"), id="queue-wait"),
    pytest.param(lambda: KeyPoolExhausted("every key muted"), id="keys-exhausted"),
    pytest.param(lambda: KeyPoolRoleRestricted("reserved above tier"), id="role-restricted"),
]


def _warm(router: Any) -> None:
    now = time.time()
    for _ in range(50):
        router._latency_profiles[A].record(now, 300.0)
        router._latency_profiles[B].record(now, 900.0)


def _outcomes(router: Any, endpoint: str) -> int:
    return router._latency_profiles[endpoint].total_count(time.time())


def _content(text: str, finish: str | None = None) -> str:
    choice: dict[str, Any] = {"index": 0, "delta": {"content": text}, "finish_reason": finish}
    return "data: " + json.dumps({"choices": [choice]}) + "\n\n"


@pytest.mark.unit
@pytest.mark.parametrize("make", REFUSALS)
def test_a_refusal_is_uncharged_and_marked_on_its_attempt(make):
    exc = make()
    adapter = MagicMock()
    adapter.config.provider = "p"
    adapter.config.endpoint_id = A

    assert is_uncharged_failure(exc)
    assert _is_routewise_retryable_error(exc)
    assert failed_attempt(adapter, exc)["uncharged"] is True


@pytest.mark.unit
def test_an_endpoint_fault_is_not_marked():
    adapter = MagicMock()
    adapter.config.provider = "p"
    adapter.config.endpoint_id = A

    assert "uncharged" not in failed_attempt(adapter, TimeoutError("read timeout"))
    assert not is_uncharged_failure(None)


@pytest.mark.unit
@pytest.mark.parametrize("make", REFUSALS)
async def test_a_refusal_is_re_solved_on_the_other_candidate(make):
    router, a, b = _make_router_with_two_api(RouteWiseConfig(random_seed=1))
    _warm(router)
    a.chat_completion = AsyncMock(side_effect=make())
    b.chat_completion = AsyncMock(
        return_value={"choices": [{"message": {"content": "hi"}}], "usage": {}}
    )

    response = await router.chat_completion("test-model", [{"role": "user", "content": "x"}])

    assert a.chat_completion.await_count == 1
    assert b.chat_completion.await_count == 1
    assert response["_routing"]["endpoint_id"] == B
    [attempt] = response["_routing"]["failed_attempts"]
    assert attempt["endpoint_id"] == A
    assert attempt["uncharged"] is True


@pytest.mark.unit
@pytest.mark.parametrize("make", REFUSALS)
async def test_a_streaming_refusal_is_re_solved_on_the_other_candidate(make):
    router, a, b = _make_router_with_two_api(RouteWiseConfig(random_seed=1))
    _warm(router)
    exc = make()

    async def refused(messages, **params):
        raise exc
        yield  # pragma: no cover - makes this an async generator

    async def served(messages, **params):
        yield _content("hi")
        yield "data: [DONE]\n\n"

    a.stream_chat_completion = refused
    b.stream_chat_completion = served

    chunks = [
        chunk
        async for chunk in router.stream_chat_completion(
            "test-model", [{"role": "user", "content": "x"}]
        )
    ]

    assert '"content": "hi"' in "".join(chunks)


@pytest.mark.unit
def test_the_observation_of_a_refusal_adds_no_sample():
    router, _a, _b = _make_router_with_two_api(RouteWiseConfig(random_seed=1))
    _warm(router)
    before_a, before_b = _outcomes(router, A), _outcomes(router, B)
    adapter = MagicMock()
    adapter.config.provider = "p"

    def attempt(endpoint: str, exc: BaseException) -> dict[str, Any]:
        adapter.config.endpoint_id = endpoint
        return failed_attempt(adapter, exc)

    # Both candidates refused; the request ended on B's refusal, which RouteWise
    # also lists among the failed attempts.
    final = UpstreamSaturated("no slot")
    routing = {
        "provider": "p",
        "endpoint_id": B,
        "failed_attempts": [attempt(A, KeyPoolExhausted("muted")), attempt(B, final)],
        "routewise": {"attempt_ttft_ms": None},
    }
    CompletionsLogger.__new__(CompletionsLogger).record_routing_observation(
        router,
        "test-model",
        routing,
        request_id="req-x",
        ttft_ms=None,
        total_latency_ms=30_000.0,
        prompt_tokens=0,
        completion_tokens=0,
        success=False,
        status_code=None,
        uncharged=is_uncharged_failure(final),
    )

    assert (_outcomes(router, A), _outcomes(router, B)) == (before_a, before_b)


@pytest.mark.unit
def test_a_charged_failure_without_a_status_still_counts():
    router, _a, _b = _make_router_with_two_api(RouteWiseConfig(random_seed=1))
    before = _outcomes(router, A)

    router.record_observation(
        RoutingObservation(
            model_id="test-model",
            endpoint_id=A,
            ttft_ms=None,
            total_latency_ms=30_000.0,
            token_count=0,
            success=False,
            status_code=None,
        )
    )

    assert _outcomes(router, A) == before + 1


@pytest.mark.unit
def test_replay_leaves_refusals_out():
    router, _a, _b = _make_router_with_two_api(RouteWiseConfig(random_seed=1))
    adapter = MagicMock()
    adapter.config.provider = "p"
    adapter.config.endpoint_id = A
    refused_a = failed_attempt(adapter, UpstreamSaturated("no slot"))
    adapter.config.endpoint_id = B
    refused_b = failed_attempt(adapter, KeyPoolExhausted("muted"))
    timed_out_a = {"endpoint_id": A, "error_type": "TimeoutError", "error": "read timeout"}
    now = time.time()
    rows = [
        # Ended on B's refusal after A's: nothing is anyone's fault.
        {
            "timestamp": now,
            "model_id": "test-model",
            "endpoint_id": B,
            "status_code": 429,
            "error": "muted",
            "failed_attempts": (refused_a, refused_b),
        },
        # An ordinary timeout replays as it always did.
        {
            "timestamp": now,
            "model_id": "test-model",
            "endpoint_id": B,
            "status_code": 200,
            "ttft_ms": 500,
            "failed_attempts": (timed_out_a,),
        },
    ]

    counts = router.bootstrap_from_log_rows(rows, include_envelope=False)

    assert counts["failed_attempts"] == 1
    assert _outcomes(router, A) == 1
    assert router._latency_profiles[B].total_count(now) == 1
    assert router._latency_profiles[B].sample_count(now) == 1


# ---------------------------------------------------------------------------
# Through the serving layer
# ---------------------------------------------------------------------------


def _session_for(router: Any) -> tuple[Any, MagicMock]:
    """A StreamSession whose observations reach ``router`` the way production's do."""
    completions_logger = MagicMock(spec=CompletionsLogger)
    completions_logger.record_routing_observation.side_effect = functools.partial(
        CompletionsLogger.record_routing_observation,
        CompletionsLogger.__new__(CompletionsLogger),
    )
    session = _make_session(
        completions_logger=completions_logger,
        routing=RoutingInfo(request_id="rid-1", model="test-model"),
    )
    session._active_router = router
    return session, completions_logger


@pytest.mark.unit
async def test_a_stream_ended_by_refusals_teaches_routewise_nothing():
    router, a, b = _make_router_with_two_api(RouteWiseConfig(random_seed=1))
    _warm(router)
    before = (_outcomes(router, A), _outcomes(router, B))

    async def refused(messages, **params):
        raise UpstreamSaturated("no slot")
        yield  # pragma: no cover - makes this an async generator

    a.stream_chat_completion = refused
    b.stream_chat_completion = refused
    session, completions_logger = _session_for(router)

    chunks = [
        chunk
        async for chunk in session.stream(
            router.stream_chat_completion("test-model", [{"role": "user", "content": "x"}])
        )
    ]

    assert chunks  # the client is told about the failure
    observed = completions_logger.record_routing_observation.call_args.kwargs
    assert observed["success"] is False
    assert observed["uncharged"] is True
    assert (_outcomes(router, A), _outcomes(router, B)) == before


@pytest.mark.unit
async def test_an_empty_stream_completion_through_the_serving_layer_adds_no_failure():
    router, a, b = _make_router_with_two_api(RouteWiseConfig(random_seed=1))
    _warm(router)
    before = (_outcomes(router, A), _outcomes(router, B))

    async def empty(messages, **params):
        yield _content("", finish="stop")
        yield "data: [DONE]\n\n"

    a.stream_chat_completion = empty
    b.stream_chat_completion = empty
    session, completions_logger = _session_for(router)

    async for _chunk in session.stream(
        router.stream_chat_completion("test-model", [{"role": "user", "content": "x"}])
    ):
        pass

    observed = completions_logger.record_routing_observation.call_args.kwargs
    assert observed["success"] is False
    assert (_outcomes(router, A), _outcomes(router, B)) == before
