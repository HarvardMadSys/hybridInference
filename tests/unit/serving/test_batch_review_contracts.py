"""Independent regression probes for Batch target integration boundaries.

These tests use existing handler/dispatch interfaces, without network or database
access. The native JSONResponse probes intentionally fail on the Chat-only base.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Response
from fastapi.responses import JSONResponse

from serving.batch import dispatch
from serving.servers.routers import anthropic_messages, responses
from tests.unit.serving.test_batch_dispatch import _body, _owner, _setup, _Upstream


@pytest.mark.parametrize("status,retryable", [(400, False), (429, True), (503, True)])
async def test_batch_preserves_native_json_error_status(monkeypatch, status, retryable):
    """Native handlers return errors rather than raising HTTPException."""
    app, services = _setup(_Upstream())
    handler = AsyncMock(
        return_value=JSONResponse(
            {"type": "error", "error": {"type": "api_error", "message": "provider failed"}},
            status_code=status,
        )
    )
    monkeypatch.setattr(anthropic_messages, "anthropic_messages", handler)
    result = await dispatch.dispatch_item(
        app=app,
        services=services,
        user_ctx=_owner(),
        body=_body(),
        batch_id="batch-review",
        target="messages",
    )
    assert result.status_code == status
    assert result.response is None
    assert result.error is not None
    assert result.retryable is retryable


async def test_batch_pinned_messages_model_cannot_use_small_budget_reroute(monkeypatch):
    """Run actual Messages handler with Batch execution context and tiny budget."""
    upstream = _Upstream()
    upstream.config.supported_params = ["thinking", "max_tokens"]
    app, services = _setup(upstream)
    enabled = AsyncMock(return_value=True)
    monkeypatch.setattr(anthropic_messages, "_reroute_enabled", enabled)
    monkeypatch.setattr(anthropic_messages, "_SMALL_MAXTOK_THRESHOLD", 128)
    result = await dispatch.dispatch_item(
        app=app,
        services=services,
        user_ctx=_owner(),
        body=_body(max_tokens=64),
        batch_id="batch-review",
        target="messages",
        protocol_profile="2023-06-01",
    )
    assert result.status_code == 200, result
    assert result.response["model"] == "batch-model"
    assert len(upstream.calls) == 1
    assert upstream.calls[0]["params"]["max_tokens"] == 64
    enabled.assert_not_awaited()


async def test_responses_explicit_no_store_survives_real_handler_translation(monkeypatch):
    """Batch must supply store=false; the real handler must not persist it."""
    app, _services = _setup(_Upstream())
    request = dispatch._request(
        app,
        json.dumps({"model": "batch-model", "input": "hello", "store": False}).encode(),
        "review-request",
        "/v1/responses",
        None,
    )
    store = SimpleNamespace(persist_enabled=True, save=AsyncMock(), get=AsyncMock())
    delegate = AsyncMock(
        return_value={
            "id": "chatcmpl-review",
            "object": "chat.completion",
            "created": 1,
            "model": "batch-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }
    )
    monkeypatch.setattr(responses, "chat_completions", delegate)
    result = await responses.create_response(
        request,
        Response(),
        authorization=None,
        user_ctx=_owner(batch_job_id="batch-review"),
        router_exec=None,
        log_store=None,
        model_router_registry=None,
        model_visibility_resolver=None,
        completions_logger=None,
        pricing_lookup=None,
        cost_tracker=None,
        response_store=store,
        _concurrency_slot=None,
    )
    payload = json.loads(result.body)
    assert result.status_code == 200
    assert payload["object"] == "response"
    assert payload["store"] is False
    assert payload["usage"]["total_tokens"] == 3
    store.save.assert_not_awaited()
    store.get.assert_not_awaited()
    assert (await request.json())["messages"] == [{"role": "user", "content": "hello"}]
    assert delegate.await_args.kwargs["user_ctx"]["batch_job_id"] == "batch-review"


async def test_messages_upstream_timeout_keeps_cost_unknown(monkeypatch):
    """A provider may finish/bill even when its response never reaches us."""
    upstream = _Upstream()
    entered = AsyncMock(side_effect=TimeoutError("response lost after submission"))
    monkeypatch.setattr(upstream, "messages", entered)
    app, services = _setup(upstream)
    result = await dispatch.dispatch_item(
        app=app,
        services=services,
        user_ctx=_owner(),
        body=_body(max_tokens=64),
        batch_id="batch-review",
        target="messages",
        protocol_profile="2023-06-01",
    )
    entered.assert_awaited_once()
    assert result.status_code == 504
    assert result.retryable is True
    assert result.cost_usd is None
    assert result.cost_status == "unknown"


@pytest.mark.parametrize("version", ["2023-06-01", "2026-10-21"])
async def test_native_batch_version_reaches_wire_without_leaking_scope(monkeypatch, version):
    from routing.routers import FixedRouter
    from serving.adapters.anthropic import AnthropicAdapter
    from serving.adapters.base import ModelConfig
    from serving.http import AsyncHTTPClient

    adapter = AnthropicAdapter(
        ModelConfig(
            id="native-review",
            name="native-review",
            provider="anthropic",
            base_url="https://anthropic.example.test",
            api_key="test-key",
        )
    )
    app, services = _setup(_Upstream())
    router = FixedRouter()
    router.register_route("native-review", [(adapter, 1.0)])
    services.router = router
    wire = AsyncMock(
        return_value={
            "id": "msg_review",
            "type": "message",
            "role": "assistant",
            "model": "native-review",
            "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }
    )
    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", wire)
    result = await dispatch.dispatch_item(
        app=app,
        services=services,
        user_ctx=_owner(),
        body={
            "model": "native-review",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        },
        batch_id="batch-review",
        target="messages",
        protocol_profile=version,
    )
    assert result.status_code == 200, result
    assert wire.await_args.kwargs["headers"]["anthropic-version"] == version
    assert adapter._upstream_headers(streaming=False)["anthropic-version"] == "2023-06-01"
