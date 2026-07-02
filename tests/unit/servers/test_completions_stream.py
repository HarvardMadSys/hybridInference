"""Tests for ``StreamSession``."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.servers.routers.completions_cost import CostTracker, PricingLookup
from serving.servers.routers.completions_logging import CompletionsLogger
from serving.servers.routers.completions_stream import (
    StreamSession,
    _ToolCallAccumulator,
    _TTFTTracker,
)
from serving.servers.routers.routing_info import RoutingInfo

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _aiter(items: list[Any]) -> AsyncIterator[Any]:
    for item in items:
        yield item


async def _consume(stream: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in stream]


def _routing(model: str = "gpt-4") -> RoutingInfo:
    return RoutingInfo(request_id="rid-1", model=model)


def _make_session(
    *,
    log_store: Any | None = None,
    cost_tracker: Any | None = None,
    completions_logger: Any | None = None,
    pricing_lookup: Any | None = None,
    is_synthetic_probe: bool = False,
    metadata: dict[str, Any] | None = None,
    routing: RoutingInfo | None = None,
    request_headers: Any | None = None,
) -> StreamSession:
    routing = routing or _routing()
    if log_store is None:
        log_store = MagicMock()
        log_store.log_request = AsyncMock()
    if completions_logger is None:
        completions_logger = MagicMock(spec=CompletionsLogger)
    if cost_tracker is None:
        cost_tracker = MagicMock(spec=CostTracker)

        async def _identity_increment(**kwargs):
            return kwargs["routing"]

        cost_tracker.schedule_increment = AsyncMock(side_effect=_identity_increment)
    if pricing_lookup is None:
        pricing_lookup = MagicMock(spec=PricingLookup)
        pricing_lookup.raw_dict_for_routing = MagicMock(return_value=None)

    return StreamSession(
        routing=routing,
        model=routing.model,
        messages=[{"role": "user", "content": "hi"}],
        params={"stream": True},
        request_id="rid-1",
        start_time=time.time(),
        request_headers=request_headers or {},
        metadata=metadata if metadata is not None else {},
        user_id="user-1",
        is_synthetic_probe=is_synthetic_probe,
        # Mirror the historical behavior: a synthetic probe suppresses logging.
        suppress_synthetic_logging=is_synthetic_probe,
        log_store=log_store,
        active_router=MagicMock(),
        cost_tracker=cost_tracker,
        completions_logger=completions_logger,
        pricing_lookup=pricing_lookup,
        get_adapter_config_for_provider=lambda _provider, _base_url: None,
    )


def _content_chunk(model: str, content: str, finish: str | None = None) -> str:
    payload = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": content},
                "finish_reason": finish,
            }
        ],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _usage_chunk(model: str, usage: dict[str, int]) -> str:
    payload = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": model,
        "choices": [],
        "usage": usage,
    }
    return f"data: {json.dumps(payload)}\n\n"


# ---------------------------------------------------------------------------
# _ToolCallAccumulator
# ---------------------------------------------------------------------------


def test_tool_call_accumulator_merges_deltas_by_index():
    acc = _ToolCallAccumulator()
    acc.add(
        [
            {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": ""},
            }
        ]
    )
    acc.add([{"index": 0, "function": {"arguments": '{"q":'}}])
    acc.add([{"index": 0, "function": {"arguments": '"hi"}'}}])
    out = acc.to_list()
    assert len(out) == 1
    assert out[0]["id"] == "call_1"
    assert out[0]["function"]["name"] == "lookup"
    assert out[0]["function"]["arguments"] == '{"q":"hi"}'


def test_tool_call_accumulator_handles_multiple_indices():
    acc = _ToolCallAccumulator()
    acc.add([{"index": 1, "id": "b", "function": {"name": "f2", "arguments": ""}}])
    acc.add([{"index": 0, "id": "a", "function": {"name": "f1", "arguments": ""}}])
    out = acc.to_list()
    # Sorted by index — deterministic order regardless of insertion order.
    assert [tc["id"] for tc in out] == ["a", "b"]


def test_tool_call_accumulator_bool_when_empty():
    assert not _ToolCallAccumulator()
    acc = _ToolCallAccumulator()
    acc.add([{"index": 0, "id": "x", "function": {}}])
    assert acc


# ---------------------------------------------------------------------------
# _TTFTTracker
# ---------------------------------------------------------------------------


def test_ttft_tracker_records_on_first_meaningful_delta():
    start = time.time() - 0.1  # 100ms ago
    t = _TTFTTracker(start)
    t.maybe_record(False)
    assert t.ttft_ms is None
    t.maybe_record(True)
    assert t.ttft_ms is not None
    first = t.ttft_ms
    # Subsequent calls don't overwrite.
    t.maybe_record(True)
    assert t.ttft_ms == first


# ---------------------------------------------------------------------------
# StreamSession.stream — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_yields_role_chunk_first_then_passthrough_chunks():
    session = _make_session()
    chunks = [_content_chunk("gpt-4", "Hello "), _content_chunk("gpt-4", "world")]
    out = await _consume(session.stream(_aiter(chunks)))
    # First yielded chunk is the role chunk.
    assert out[0].startswith("data: ")
    payload = json.loads(out[0][6:])
    assert payload["choices"][0]["delta"] == {"role": "assistant"}
    # Followed by the two content chunks (sanitized but byte-identical here).
    assert out[1] == chunks[0]
    assert out[2] == chunks[1]


@pytest.mark.asyncio
async def test_yielded_first_chunk_flag_flips_after_first_yield():
    session = _make_session()
    assert session.yielded_first_chunk is False
    chunks = [_content_chunk("gpt-4", "x")]
    # Pull the first item only.
    gen = session.stream(_aiter(chunks))
    first = await gen.__anext__()
    assert first  # role chunk
    assert session.yielded_first_chunk is True
    # Drain the rest so the generator finalizes cleanly.
    async for _ in gen:
        pass


@pytest.mark.asyncio
async def test_finalization_schedules_log_and_records_observation():
    log_store = MagicMock()
    log_store.log_request = AsyncMock()
    cl_logger = MagicMock(spec=CompletionsLogger)
    session = _make_session(
        log_store=log_store,
        completions_logger=cl_logger,
    )
    chunks = [_content_chunk("gpt-4", "hi", finish="stop")]
    await _consume(session.stream(_aiter(chunks)))

    cl_logger.schedule_log.assert_called_once()
    request_id, log_data = cl_logger.schedule_log.call_args.args
    assert request_id == "rid-1"
    assert log_data["status_code"] == 200
    assert log_data["response"]["choices"][0]["message"]["content"] == "hi"
    assert log_data["response"]["choices"][0]["finish_reason"] == "stop"
    cl_logger.record_routing_observation.assert_called_once()
    obs_kwargs = cl_logger.record_routing_observation.call_args.kwargs
    assert obs_kwargs["success"] is True


@pytest.mark.asyncio
async def test_finalization_flags_empty_completion_and_skips_routewise_reward():
    """A 200 stream that ends with no content is not a routing win.

    Seen in prod on zai/minimax/local-sglang routes: a well-formed stream
    with an empty message and finish_reason "stop" -- no exception raised,
    but nothing delivered either. Regression for treating that identically
    to a normal completion (rewarding RouteWise, no observability signal).
    """
    cl_logger = MagicMock(spec=CompletionsLogger)
    session = _make_session(completions_logger=cl_logger)
    chunks = [_content_chunk("gpt-4", "", finish="stop")]
    await _consume(session.stream(_aiter(chunks)))

    log_data = cl_logger.schedule_log.call_args.args[1]
    assert log_data["response"]["choices"][0]["message"]["content"] is None
    assert log_data["metadata"]["empty_completion"] is True
    obs_kwargs = cl_logger.record_routing_observation.call_args.kwargs
    assert obs_kwargs["success"] is False


@pytest.mark.asyncio
async def test_finalization_tool_call_only_response_not_flagged_empty():
    """A tool-call-only response (no text) is a real completion, not empty."""
    cl_logger = MagicMock(spec=CompletionsLogger)
    session = _make_session(completions_logger=cl_logger)

    def _tc_chunk(deltas: list[dict[str, Any]], finish: str | None = None) -> str:
        payload = {
            "id": "x",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-4",
            "choices": [{"index": 0, "delta": {"tool_calls": deltas}, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload)}\n\n"

    chunks = [
        _tc_chunk(
            [
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
            finish="tool_calls",
        )
    ]
    await _consume(session.stream(_aiter(chunks)))

    log_data = cl_logger.schedule_log.call_args.args[1]
    assert "empty_completion" not in log_data["metadata"]
    obs_kwargs = cl_logger.record_routing_observation.call_args.kwargs
    assert obs_kwargs["success"] is True


@pytest.mark.asyncio
async def test_finalization_propagates_usage_and_schedules_cost_when_routing_present():
    cl_logger = MagicMock(spec=CompletionsLogger)
    cost_tracker = MagicMock(spec=CostTracker)

    async def _ident(**kwargs):
        return kwargs["routing"]

    cost_tracker.schedule_increment = AsyncMock(side_effect=_ident)

    # Adapter chunk that surfaces ``_routing`` so the finalizer takes the
    # main branch (cost-increment scheduled, pricing looked up).
    routed_chunk = (
        'data: {"id": "x", "object": "chat.completion.chunk", "created": 1, '
        '"model": "gpt-4", "choices": [{"index": 0, "delta": {"content": "ok"}, '
        '"finish_reason": "stop"}], '
        '"_routing": {"provider": "openai", "base_url": "https://api.openai.com/v1"}}\n\n'
    )
    usage_only = _usage_chunk("gpt-4", {"prompt_tokens": 5, "completion_tokens": 2})

    session = _make_session(cost_tracker=cost_tracker, completions_logger=cl_logger)
    await _consume(session.stream(_aiter([routed_chunk, usage_only])))

    cost_tracker.schedule_increment.assert_awaited_once()
    inc_kwargs = cost_tracker.schedule_increment.call_args.kwargs
    assert inc_kwargs["prompt_tokens"] == 5
    assert inc_kwargs["completion_tokens"] == 2
    cl_logger.schedule_log.assert_called_once()
    log_data = cl_logger.schedule_log.call_args.args[1]
    assert log_data["provider"] == "openai"
    assert log_data["usage"]["prompt_tokens"] == 5


@pytest.mark.asyncio
async def test_finalization_skipped_for_synthetic_probe():
    log_store = MagicMock()
    log_store.log_request = AsyncMock()
    cl_logger = MagicMock(spec=CompletionsLogger)
    cost_tracker = MagicMock(spec=CostTracker)
    cost_tracker.schedule_increment = AsyncMock()

    session = _make_session(
        log_store=log_store,
        cost_tracker=cost_tracker,
        completions_logger=cl_logger,
        is_synthetic_probe=True,
    )
    await _consume(session.stream(_aiter([_content_chunk("gpt-4", "hi", finish="stop")])))

    cl_logger.schedule_log.assert_not_called()
    cl_logger.record_routing_observation.assert_not_called()
    cost_tracker.schedule_increment.assert_not_awaited()


# ---------------------------------------------------------------------------
# StreamSession — TTFT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ttft_recorded_on_first_content_chunk():
    cl_logger = MagicMock(spec=CompletionsLogger)
    session = _make_session(completions_logger=cl_logger)
    chunks = [_content_chunk("gpt-4", "first"), _content_chunk("gpt-4", "second")]
    await _consume(session.stream(_aiter(chunks)))

    log_data = cl_logger.schedule_log.call_args.args[1]
    assert log_data["ttft_ms"] is not None
    assert log_data["ttft_ms"] >= 0


# ---------------------------------------------------------------------------
# StreamSession — tool calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_deltas_merged_into_db_response():
    cl_logger = MagicMock(spec=CompletionsLogger)
    session = _make_session(completions_logger=cl_logger)

    def _tc_chunk(deltas: list[dict[str, Any]], finish: str | None = None) -> str:
        payload = {
            "id": "x",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-4",
            "choices": [{"index": 0, "delta": {"tool_calls": deltas}, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload)}\n\n"

    chunks = [
        _tc_chunk(
            [
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": ""},
                }
            ]
        ),
        _tc_chunk([{"index": 0, "function": {"arguments": '{"q":"hi"}'}}], finish="tool_calls"),
    ]
    await _consume(session.stream(_aiter(chunks)))

    log_data = cl_logger.schedule_log.call_args.args[1]
    message = log_data["response"]["choices"][0]["message"]
    assert message["content"] is None
    assert message["tool_calls"] == [
        {
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "lookup", "arguments": '{"q":"hi"}'},
        }
    ]
    assert log_data["response"]["choices"][0]["finish_reason"] == "tool_calls"


# ---------------------------------------------------------------------------
# StreamSession — error path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_emits_error_chunk_and_schedules_error_log():
    cl_logger = MagicMock(spec=CompletionsLogger)
    log_store = MagicMock()
    log_store.log_request = AsyncMock()
    session = _make_session(log_store=log_store, completions_logger=cl_logger)

    async def _gen():
        yield _content_chunk("gpt-4", "partial")
        raise RuntimeError("upstream blew up")

    out = await _consume(session.stream(_gen()))
    # role chunk + content chunk + error chunk
    assert len(out) == 3
    assert "error" in out[-1]
    error_payload = json.loads(out[-1][6:])
    assert error_payload["error"]["code"] == 500
    cl_logger.schedule_log.assert_called_once()
    log_data = cl_logger.schedule_log.call_args.args[1]
    assert log_data["status_code"] == 500
    assert log_data["error"] == "upstream blew up"
    cl_logger.record_routing_observation.assert_called_once()
    obs_kwargs = cl_logger.record_routing_observation.call_args.kwargs
    assert obs_kwargs["success"] is False


@pytest.mark.asyncio
async def test_error_log_includes_routewise_metadata_from_exception():
    cl_logger = MagicMock(spec=CompletionsLogger)
    session = _make_session(completions_logger=cl_logger, metadata={"user_id": "user-1"})
    routewise = {
        "selected_provider_type": "on_demand",
        "selected_provider": "openai",
        "gain_c": float("-inf"),
        "hedging_triggered": True,
        "hedge_backup_provider": "anthropic",
    }

    async def _gen():
        exc = RuntimeError("upstream blew up")
        exc._routing = {"provider": "openai", "routewise": routewise}
        raise exc
        if False:  # pragma: no cover
            yield ""

    await _consume(session.stream(_gen()))

    log_data = cl_logger.schedule_log.call_args.args[1]
    # The real upstream provider from ``exc._routing`` must land on the provider
    # column (not the "router" sentinel), else the error is hidden from the
    # provider-performance aggregations.
    assert log_data["provider"] == "openai"
    assert log_data["metadata"]["user_id"] == "user-1"
    assert log_data["metadata"]["routewise"] == {**routewise, "gain_c": None}


@pytest.mark.asyncio
async def test_error_provider_recovered_from_routing_chunk_when_exc_has_no_routing():
    """A mid-stream failure without ``exc._routing`` still attributes the provider.

    When a ``_routing`` chunk arrived before the failure, the provider captured
    from it is used rather than the "router" sentinel.
    """
    cl_logger = MagicMock(spec=CompletionsLogger)
    routed_chunk = (
        'data: {"id": "x", "object": "chat.completion.chunk", "created": 1, '
        '"model": "gpt-4", "choices": [{"index": 0, "delta": {"content": "ok"}}], '
        '"_routing": {"provider": "openai", "base_url": "https://api.openai.com/v1"}}\n\n'
    )
    session = _make_session(completions_logger=cl_logger)

    async def _gen():
        yield routed_chunk
        raise RuntimeError("mid-stream boom")  # note: no exc._routing attached

    await _consume(session.stream(_gen()))

    log_data = cl_logger.schedule_log.call_args.args[1]
    assert log_data["provider"] == "openai"


@pytest.mark.asyncio
async def test_error_before_any_chunk_still_emits_role_chunk_then_error():
    cl_logger = MagicMock(spec=CompletionsLogger)
    session = _make_session(completions_logger=cl_logger)

    async def _gen():
        raise RuntimeError("connection refused")
        if False:  # pragma: no cover
            yield ""

    out = await _consume(session.stream(_gen()))
    # The role chunk yields BEFORE we touch the adapter generator, so it
    # always appears even when the adapter raises immediately.
    assert len(out) == 2
    role_payload = json.loads(out[0][6:])
    assert role_payload["choices"][0]["delta"] == {"role": "assistant"}
    assert "error" in out[1]
    log_data = cl_logger.schedule_log.call_args.args[1]
    assert log_data["status_code"] == 500
    # TTFT was never set since no meaningful delta arrived.
    assert log_data["ttft_ms"] is None


@pytest.mark.asyncio
async def test_cancellation_mid_stream_persists_failure_log_and_reraises():
    """A timeout/disconnect (CancelledError) must not drop the failed request.

    ``asyncio.CancelledError`` is a ``BaseException``, so it bypasses the
    ``except Exception`` error branch. Without dedicated handling the failed
    request skipped finalization entirely — no ``api_logs`` row — so under load
    every timed-out request vanished. The failure row must still be scheduled,
    and the cancellation must propagate (never be swallowed).
    """
    cl_logger = MagicMock(spec=CompletionsLogger)
    session = _make_session(completions_logger=cl_logger)

    async def _slow():
        yield _content_chunk("gpt-4", "partial")
        await asyncio.sleep(10)  # suspends here until cancelled

    gen = session.stream(_slow())
    await gen.__anext__()  # role chunk
    await gen.__anext__()  # forwarded content chunk

    # Simulate the response task being cancelled (request timeout / disconnect)
    # while the generator is suspended awaiting the next upstream chunk.
    with pytest.raises(asyncio.CancelledError):
        await gen.athrow(asyncio.CancelledError())

    cl_logger.schedule_log.assert_called_once()
    log_data = cl_logger.schedule_log.call_args.args[1]
    assert log_data["status_code"] == 500
    # Message-less CancelledError still records an identifiable error string.
    assert log_data["error"]
    cl_logger.record_routing_observation.assert_called_once()
    assert cl_logger.record_routing_observation.call_args.kwargs["success"] is False


@pytest.mark.asyncio
async def test_client_disconnect_midstream_persists_failure_log():
    """A client disconnect (generator ``aclose`` → ``GeneratorExit``) still logs.

    ``GeneratorExit`` is a ``BaseException`` too, so like a cancellation it must
    not silently drop the in-flight request from ``api_logs``.
    """
    cl_logger = MagicMock(spec=CompletionsLogger)
    session = _make_session(completions_logger=cl_logger)

    async def _slow():
        yield _content_chunk("gpt-4", "partial")
        await asyncio.sleep(10)  # suspends here until the consumer goes away

    gen = session.stream(_slow())
    await gen.__anext__()  # role chunk
    await gen.__anext__()  # forwarded content chunk

    # Consumer abandons the stream: closing the generator raises GeneratorExit
    # at the suspended await.
    await gen.aclose()

    cl_logger.schedule_log.assert_called_once()
    assert cl_logger.schedule_log.call_args.args[1]["status_code"] == 500


@pytest.mark.asyncio
async def test_failure_log_scheduled_even_when_observation_raises():
    """A throwing routing-observation update must not drop the error log.

    ``record_routing_observation`` runs before the DB log is scheduled; an
    online-learning router's ``record_observation`` does real work and can
    raise. If it did, the failed request must still be persisted.
    """
    cl_logger = MagicMock(spec=CompletionsLogger)
    cl_logger.record_routing_observation.side_effect = RuntimeError("observation boom")
    session = _make_session(completions_logger=cl_logger)

    async def _gen():
        raise RuntimeError("upstream blew up")
        if False:  # pragma: no cover
            yield ""

    out = await _consume(session.stream(_gen()))

    # The client still receives the error chunk...
    assert "error" in out[-1]
    # ...and the failed request is still persisted despite the observation throw.
    cl_logger.schedule_log.assert_called_once()
    log_data = cl_logger.schedule_log.call_args.args[1]
    assert log_data["status_code"] == 500
    assert log_data["error"] == "upstream blew up"


# ---------------------------------------------------------------------------
# StreamSession — keepalive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keepalive_emitted_during_idle_window(monkeypatch):
    """Idle stream emits SSE keepalive comments without dropping the upstream."""
    session = _make_session()

    real_wait_for = asyncio.wait_for

    async def fast_wait_for(awaitable, timeout=None):
        shortened = 0.01 if timeout is not None and timeout > 0.01 else timeout
        return await real_wait_for(awaitable, timeout=shortened)

    # Patch the module-level asyncio so the keepalive timer fires near-instantly.
    monkeypatch.setattr(
        "serving.servers.routers.completions_stream.asyncio.wait_for", fast_wait_for
    )

    async def _slow():
        await asyncio.sleep(0.05)
        yield _content_chunk("gpt-4", "late", finish="stop")

    out = await _consume(session.stream(_slow()))
    assert any(line == ": keepalive\n\n" for line in out)
    # Final content still delivered after keepalive(s).
    data_payloads = [
        json.loads(line[6:])
        for line in out
        if line.startswith("data: ") and not line.startswith("data: [DONE]")
    ]
    contents = [
        p["choices"][0]["delta"].get("content")
        for p in data_payloads
        if p.get("choices") and p["choices"][0].get("delta", {}).get("content") is not None
    ]
    assert "late" in contents


# ---------------------------------------------------------------------------
# StreamSession — non-JSON / [DONE] passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_done_sentinel_passes_through_unchanged():
    session = _make_session()
    chunks = [_content_chunk("gpt-4", "hi", finish="stop"), "data: [DONE]\n\n"]
    out = await _consume(session.stream(_aiter(chunks)))
    assert out[-1] == "data: [DONE]\n\n"
