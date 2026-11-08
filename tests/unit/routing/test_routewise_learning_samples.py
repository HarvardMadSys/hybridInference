"""What RouteWise learns from a request: per-attempt evidence a caller cannot steer.

The LP reads each endpoint's mean TTFT, so every sample a caller can shape is a
lever on where everyone's traffic goes. Before this rule, three non-streaming
requests asking for long generations (their wall time read as TTFT), or two
streams ended at their first word by a stop sequence (an "empty completion"
read as a 60 s failure), moved the LP from the cheap endpoint to one costing a
third more, for the whole 15-minute window. A winning hedge backup was charged
the delay before it was started, and a fallback winner the time of the attempt
before it.

RouteWise now learns only the attempt's own time to first content, measured
from that attempt's dispatch (a hedge leg's from its own start); a request with
no first token is an untimed success, and a failure of an attempt the router
saw complete is the serving layer's verdict on the response, not an outage.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from routing.routewise.config import RouteWiseConfig
from serving.servers.routers.completions_logging import CompletionsLogger
from tests.unit.routing.test_routewise_router import _make_router_with_two_api

A = "test-model:api-a"  # cheaper (3/15 per M)
B = "test-model:api-b"  # pricier (4/20 per M)


def _warm(router: Any) -> None:
    now = time.time()
    for _ in range(300):
        router._latency_profiles[A].record(now, 400.0)
        router._latency_profiles[B].record(now, 600.0)
    for _ in range(25):
        router.predictor.update("test-model", 1000, 512)


def _weights(router: Any) -> dict[str, float]:
    router._select_decision("test-model", {"prompt_tokens": 1000, "params": {}}).release()
    return router._last_lp_weights["test-model"]


def _observe(router: Any, routing: dict[str, Any], **kw: Any) -> None:
    """Feed an observation exactly the way the completions handlers do."""
    CompletionsLogger.__new__(CompletionsLogger).record_routing_observation(
        router,
        "test-model",
        routing,
        request_id="req-x",
        **kw,
    )


def _routing(endpoint_id: str, **routewise: Any) -> dict[str, Any]:
    return {"provider": "p", "endpoint_id": endpoint_id, "routewise": routewise}


def _content(text: str) -> str:
    return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}) + "\n\n"


def _decision_routing(chunks: list[Any]) -> dict[str, Any]:
    """Return the trailing ``_routing`` block a RouteWise stream ends with."""
    blocks = [
        json.loads(chunk[6:])["_routing"]
        for chunk in chunks
        if isinstance(chunk, str) and chunk.startswith("data: ") and '"routewise"' in chunk
    ]
    assert blocks, "a RouteWise stream ends with its decision metadata"
    return blocks[-1]


# ---------------------------------------------------------------------------
# Live observations
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_long_non_streaming_requests_do_not_move_the_lp():
    router, _a, _b = _make_router_with_two_api(RouteWiseConfig(random_seed=1))
    _warm(router)
    assert _weights(router) == {A: 1.0}

    # A non-streaming success: no TTFT, a 30 s wall time the caller asked for.
    for _ in range(50):
        _observe(
            router,
            _routing(A, attempt_ttft_ms=None, attempt_completed=True),
            ttft_ms=None,
            total_latency_ms=30_000.0,
            prompt_tokens=20,
            completion_tokens=4000,
            success=True,
        )

    assert _weights(router) == {A: 1.0}
    profile = router._latency_profiles[A]
    assert profile.untimed_success_count(time.time()) == 50
    assert router._mean_ttft_sec(A, time.time()) == pytest.approx(0.4)


@pytest.mark.unit
def test_a_success_without_decision_metadata_never_reads_wall_time_as_ttft():
    router, _a, _b = _make_router_with_two_api(RouteWiseConfig(random_seed=1))
    _warm(router)

    for _ in range(50):
        _observe(
            router,
            {"provider": "p", "endpoint_id": A},
            ttft_ms=None,
            total_latency_ms=30_000.0,
            prompt_tokens=20,
            completion_tokens=4000,
            success=True,
        )

    assert _weights(router) == {A: 1.0}


@pytest.mark.unit
def test_an_empty_stream_completion_is_not_a_latency_failure():
    router, _a, _b = _make_router_with_two_api(RouteWiseConfig(random_seed=1))
    _warm(router)
    before = router._latency_profiles[A].total_count(time.time())

    # completions_stream: a well-formed 200 stream with no content (a stop
    # sequence matching the first word) is reported with success=False.
    for _ in range(20):
        _observe(
            router,
            _routing(A, attempt_ttft_ms=None, attempt_completed=True),
            ttft_ms=None,
            total_latency_ms=300.0,
            prompt_tokens=12,
            completion_tokens=1,
            success=False,
        )

    assert router._latency_profiles[A].total_count(time.time()) == before
    assert _weights(router) == {A: 1.0}


@pytest.mark.unit
def test_a_failed_attempt_still_counts():
    """The attempt raised: that is the endpoint's failure, whatever else applies."""
    router, _a, _b = _make_router_with_two_api(RouteWiseConfig(random_seed=1))
    before = router._latency_profiles[A].total_count(time.time())

    _observe(
        router,
        _routing(A, attempt_ttft_ms=None),
        ttft_ms=None,
        total_latency_ms=300.0,
        prompt_tokens=0,
        completion_tokens=0,
        success=False,
    )

    assert router._latency_profiles[A].total_count(time.time()) == before + 1


@pytest.mark.unit
def test_the_routers_attempt_ttft_wins_over_the_serving_clock():
    router, _a, _b = _make_router_with_two_api(RouteWiseConfig(random_seed=1))

    _observe(
        router,
        _routing(A, attempt_ttft_ms=120.0, attempt_completed=True),
        # The serving layer's clock started at request entry and kept running
        # through gateway work and an earlier attempt.
        ttft_ms=4_000.0,
        total_latency_ms=9_000.0,
        prompt_tokens=100,
        completion_tokens=10,
        success=True,
    )

    assert router._mean_ttft_sec(A, time.time()) == pytest.approx(0.12)


# ---------------------------------------------------------------------------
# The router's own measurement
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_stream_is_timed_from_its_own_dispatch():
    router, a, b = _make_router_with_two_api(RouteWiseConfig(random_seed=1))
    _warm(router)

    async def stream(messages, **params):
        await asyncio.sleep(0.05)
        yield _content("hi")
        yield "data: [DONE]\n\n"

    a.stream_chat_completion = stream
    b.stream_chat_completion = stream
    chunks = [
        chunk
        async for chunk in router.stream_chat_completion(
            "test-model", [{"role": "user", "content": "x"}]
        )
    ]

    routewise = _decision_routing(chunks)["routewise"]
    assert routewise["attempt_completed"] is True
    assert 40.0 <= routewise["attempt_ttft_ms"] < 1_000.0


@pytest.mark.unit
async def test_an_empty_stream_carries_no_attempt_ttft():
    router, a, b = _make_router_with_two_api(RouteWiseConfig(random_seed=1))
    _warm(router)

    async def stream(messages, **params):
        yield 'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        yield "data: [DONE]\n\n"

    a.stream_chat_completion = stream
    b.stream_chat_completion = stream
    chunks = [
        chunk
        async for chunk in router.stream_chat_completion(
            "test-model", [{"role": "user", "content": "x"}]
        )
    ]

    routewise = _decision_routing(chunks)["routewise"]
    assert routewise["attempt_completed"] is True
    assert routewise["attempt_ttft_ms"] is None


@pytest.mark.unit
async def test_a_non_streaming_request_has_no_attempt_ttft():
    router, a, b = _make_router_with_two_api(RouteWiseConfig(random_seed=1))
    _warm(router)

    async def chat(messages, **params):
        await asyncio.sleep(0.05)
        return {"choices": [{"message": {"content": "hi"}}], "usage": {}}

    a.chat_completion = chat
    b.chat_completion = chat
    response = await router.chat_completion("test-model", [{"role": "user", "content": "x"}])

    routewise = response["_routing"]["routewise"]
    assert routewise["attempt_completed"] is True
    assert routewise["attempt_ttft_ms"] is None


@pytest.mark.unit
async def test_a_hedge_winner_is_not_charged_the_hedge_delay():
    router, a, b = _make_router_with_two_api(
        RouteWiseConfig(
            budget_alpha=0.0,
            latency_slo_sec=0.2,
            latency_hedge_mode="probability_target",
        )
    )
    now = time.time()
    router._latency_profiles[A].record(now, 100.0)
    router._latency_profiles[B].record(now, 1.0)

    async def slow_primary(messages, **params):
        await asyncio.sleep(5)
        yield _content("primary")

    async def backup(messages, **params):
        await asyncio.sleep(0.03)
        yield _content("backup")
        yield "data: [DONE]\n\n"

    a.stream_chat_completion = slow_primary
    b.stream_chat_completion = backup
    started = time.monotonic()
    chunks = [
        chunk
        async for chunk in router.stream_chat_completion(
            "test-model", [{"role": "user", "content": "x"}]
        )
    ]
    elapsed_ms = (time.monotonic() - started) * 1000.0

    routewise = _decision_routing(chunks)["routewise"]
    assert routewise["backup_won"] is True
    # The backup was started at a checkpoint well into the request, and its own
    # first content came about 30 ms after that.
    assert routewise["hedge_delay_ms"] >= 50.0
    assert elapsed_ms >= routewise["hedge_delay_ms"]
    assert 20.0 <= routewise["attempt_ttft_ms"] < routewise["hedge_delay_ms"]

    # The serving layer merges every routing chunk of the stream: the endpoint
    # from the hedge's winner chunk, the decision metadata from the last one.
    _observe(
        router,
        _routing(B, **routewise),
        ttft_ms=elapsed_ms,
        total_latency_ms=elapsed_ms,
        prompt_tokens=10,
        completion_tokens=1,
        success=True,
    )
    profile = router._latency_profiles[B]
    assert profile.sample_count(time.time()) == 2
    assert profile.mean_ttft_sec(time.time()) == pytest.approx(
        (1.0 + routewise["attempt_ttft_ms"]) / 2 / 1000.0
    )


# ---------------------------------------------------------------------------
# Startup replay applies the same rule
# ---------------------------------------------------------------------------


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "timestamp": time.time(),
        "model_id": "test-model",
        "endpoint_id": A,
        "ttft_ms": None,
        "latency_ms": 30_000,
        "status_code": 200,
        "error": None,
        "prompt_tokens": 20,
        "completion_tokens": 4000,
        "failed_attempts": (),
    }
    row.update(overrides)
    return row


@pytest.mark.unit
def test_replay_reads_a_non_streaming_row_as_an_untimed_success():
    router, _a, _b = _make_router_with_two_api(RouteWiseConfig(random_seed=1))

    router.bootstrap_from_log_rows([_row()], include_envelope=False)

    profile = router._latency_profiles[A]
    now = time.time()
    assert profile.sample_count(now) == 0
    assert profile.untimed_success_count(now) == 1
    assert A not in router._latency_history_priors_ms


@pytest.mark.unit
def test_replay_prefers_the_routers_attempt_ttft():
    router, _a, _b = _make_router_with_two_api(RouteWiseConfig(random_seed=1))

    router.bootstrap_from_log_rows(
        [_row(ttft_ms=950, attempt_ttft_ms=30.0), _row(ttft_ms=200)],
        include_envelope=False,
    )

    profile = router._latency_profiles[A]
    assert profile.mean_ttft_sec(time.time()) == pytest.approx((30.0 + 200.0) / 2 / 1000.0)
