"""Tests for the upstream scheduling priority stamped on sglang routes.

sglang orders its waiting queue by the request's ``priority`` field when the
server runs with ``--enable-priority-scheduling``, so this is how the gateway
keeps a mega-prefill from being admitted ahead of the interactive traffic behind
it. Two invariants matter more than the plumbing: the field reaches only the
routes that declared the server understands it, and the value is the router's,
never the caller's.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.utils import context as req_ctx

_RESPONSE = {
    "choices": [{"message": {"role": "assistant", "content": "42"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
}


@pytest.fixture(autouse=True)
def _reset_req_ctx():
    req_ctx.set({})
    yield
    req_ctx.set({})


def _adapter(*, priority_scheduling: bool) -> OpenAICompatAdapter:
    config = ModelConfig(
        id="deepseek-v4-flash",
        name="DeepSeek V4 Flash",
        provider="sglang",
        base_url="http://mock.local/v1",
        provider_model_id="deepseek-v4-flash",
        supported_params=["temperature", "top_p", "max_tokens"],
        priority_scheduling=priority_scheduling,
    )
    adapter = OpenAICompatAdapter(config)
    adapter.http = MagicMock()
    return adapter


async def _sent_payload(adapter: OpenAICompatAdapter, **params: Any) -> dict[str, Any]:
    """Run a non-streaming completion and return the body that went upstream."""
    mock_post = AsyncMock(return_value=_RESPONSE)
    adapter._post_with_pool = mock_post
    await adapter.chat_completion([{"role": "user", "content": "hi"}], **params)
    return mock_post.call_args.args[1]


async def _sent_stream_payload(adapter: OpenAICompatAdapter, **params: Any) -> dict[str, Any]:
    """Run a streaming completion and return the body that went upstream."""
    captured: dict[str, Any] = {}

    async def _stream_post(*, url, json, headers, timeout):
        captured.update(json)
        yield "data: [DONE]"

    adapter.http.stream_post = _stream_post
    async for _ in adapter.stream_chat_completion([{"role": "user", "content": "hi"}], **params):
        pass
    return captured


@pytest.mark.unit
@pytest.mark.asyncio
async def test_priority_reaches_upstream_when_route_declares_the_flag():
    req_ctx.set({req_ctx.UPSTREAM_PRIORITY: 20})

    payload = await _sent_payload(_adapter(priority_scheduling=True))

    assert payload["priority"] == 20


@pytest.mark.unit
@pytest.mark.asyncio
async def test_priority_reaches_upstream_on_the_streaming_path():
    # Streaming carries the traffic this feature exists for, and it builds its
    # payload separately -- so it gets its own assertion rather than trusting
    # the non-streaming twin. Priority 0 also pins that the elephant tier is
    # sent rather than skipped as falsy.
    req_ctx.set({req_ctx.UPSTREAM_PRIORITY: 0})

    payload = await _sent_stream_payload(_adapter(priority_scheduling=True))

    assert payload["priority"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_without_the_flag_never_sees_the_field():
    """A remote provider that validates its request body must not receive it.

    Every model with a local sglang route also has a remote fallback, and the
    two share this adapter; only the route that declared the flag may be sent a
    field its API never agreed to.
    """
    req_ctx.set({req_ctx.UPSTREAM_PRIORITY: 20})

    payload = await _sent_payload(_adapter(priority_scheduling=False))
    stream_payload = await _sent_stream_payload(_adapter(priority_scheduling=False))

    assert "priority" not in payload
    assert "priority" not in stream_payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_router_priority_leaves_upstream_default_alone():
    # Direct adapter calls (warmup probes, the admin playground) run with no
    # router around them. Inventing a priority there would rank traffic the
    # policy never looked at.
    payload = await _sent_payload(_adapter(priority_scheduling=True))

    assert "priority" not in payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_cannot_choose_its_own_priority():
    """The value ranks a request by the cost it imposes, so its sender doesn't set it.

    ``validate_params`` already whitelists sampling params, which is what drops a
    client-supplied ``priority``; this pins that it stays dropped, and that the
    router's value is what actually goes out.
    """
    req_ctx.set({req_ctx.UPSTREAM_PRIORITY: 0})

    payload = await _sent_payload(_adapter(priority_scheduling=True), priority=99)
    off_payload = await _sent_payload(_adapter(priority_scheduling=False), priority=99)

    assert payload["priority"] == 0
    assert "priority" not in off_payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_integer_priority_is_ignored():
    # bool is an int subclass in Python, so a stray True would otherwise reach
    # sglang as priority=1 and quietly rank that request above an elephant.
    for bad in (True, "20", 20.5, None):
        req_ctx.set({req_ctx.UPSTREAM_PRIORITY: bad})

        payload = await _sent_payload(_adapter(priority_scheduling=True))

        assert "priority" not in payload, bad


@pytest.mark.unit
def test_priority_scheduling_defaults_off():
    # The field is meaningless to every upstream except a flag-started sglang
    # server, so the default has to be "do not send it".
    config = ModelConfig(id="m", name="m", provider="p", base_url="http://x")

    assert config.priority_scheduling is False
