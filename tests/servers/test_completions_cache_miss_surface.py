"""What a licensed route's reported cache miss looks like on the public API.

``null_cache_details_means_miss`` turns an sglang prefix-cache miss into an
explicit 0 instead of silence. The two public surfaces do not render that 0
identically, and both are worth pinning:

* non-streaming responses pass through ``ChatCompletionResponse``, whose
  ``Usage`` model declares ``cache_read_tokens`` and drops everything else, so
  ``cached_tokens`` / ``prompt_tokens_details`` never reach the client;
* streaming chunks are assembled by hand and carry all three.

Both are driven end to end here rather than asserted on ``UsageInfo.to_dict()``,
because the response model is exactly what makes them differ.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import completions

# Verbatim sglang usage for a cold prompt (h200a :8003, 2026-08-28). The miss is
# the null block; there is no `cached_tokens: 0` anywhere in it.
_COLD_USAGE = {
    "prompt_tokens": 4817,
    "total_tokens": 4818,
    "completion_tokens": 1,
    "prompt_tokens_details": None,
    "reasoning_tokens": 0,
}

_UPSTREAM_RESPONSE = {
    "id": "chatcmpl-cold",
    "choices": [{"message": {"role": "assistant", "content": "pad"}, "finish_reason": "stop"}],
    "usage": _COLD_USAGE,
}


def _adapter(*, licensed: bool) -> OpenAICompatAdapter:
    config = ModelConfig(
        id="deepseek-v4-flash",
        name="DeepSeek V4 Flash",
        provider="sglang",
        base_url="http://mock.local/v1",
        provider_model_id="deepseek-v4-flash",
        supported_params=["temperature", "top_p", "max_tokens", "stream"],
        include_usage_in_stream=True,
        null_cache_details_means_miss=licensed,
    )
    adapter = OpenAICompatAdapter(config)
    adapter.http = MagicMock()
    adapter._post_with_pool = AsyncMock(return_value=_UPSTREAM_RESPONSE)
    return adapter


def _install_stream(adapter: OpenAICompatAdapter) -> None:
    """Replay an sglang stream whose final chunk carries the cold usage."""
    content_chunk = {
        "id": "chatcmpl-cold",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "deepseek-v4-flash",
        "choices": [{"index": 0, "delta": {"content": "pad"}, "finish_reason": "stop"}],
    }
    usage_chunk = {
        "id": "chatcmpl-cold",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "deepseek-v4-flash",
        "choices": [],
        "usage": _COLD_USAGE,
    }

    async def _stream_post(**_kwargs):
        yield f"data: {json.dumps(content_chunk)}\n\n"
        yield f"data: {json.dumps(usage_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    adapter.http.stream_post = _stream_post


@pytest.fixture
async def client_for(monkeypatch, mock_db_logger, mock_log_store):
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    monkeypatch.setattr("serving.servers.auth.is_user_auth_enabled", lambda: False)

    clients: list[AsyncClient] = []

    async def _build(*, licensed: bool) -> tuple[AsyncClient, OpenAICompatAdapter]:
        adapter = _adapter(licensed=licensed)
        _install_stream(adapter)
        router = RouteExecutor()
        router.register_route("deepseek-v4-flash", [(adapter, 1.0)])

        app = FastAPI()
        app.state.services = AppServices(
            router=router, db_logger=mock_db_logger, log_store=mock_log_store
        )
        install_error_handlers(app)
        app.include_router(completions.router)

        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        clients.append(client)
        return client, adapter

    yield _build

    for client in clients:
        await client.aclose()


async def _post(client: AsyncClient, *, stream: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "hi"}],
    }
    if stream:
        body["stream"] = True
    resp = await client.post("/v1/chat/completions", json=body)
    assert resp.status_code == status.HTTP_200_OK

    if not stream:
        return resp.json()["usage"]

    usage: dict[str, Any] = {}
    for line in resp.text.splitlines():
        if not line.startswith("data: ") or line.startswith("data: [DONE]"):
            continue
        chunk = json.loads(line[6:])
        if chunk.get("usage"):
            usage = chunk["usage"]
    return usage


@pytest.mark.asyncio
async def test_non_streaming_surfaces_the_zero_as_cache_read_tokens_only(client_for):
    client, _ = await client_for(licensed=True)

    usage = await _post(client, stream=False)

    assert usage["cache_read_tokens"] == 0
    # Filtered by the Usage response model, so the OpenAI-nested spelling of the
    # miss does not appear on this surface.
    assert "cached_tokens" not in usage
    assert "prompt_tokens_details" not in usage


@pytest.mark.asyncio
async def test_streaming_surfaces_the_zero_in_all_three_spellings(client_for):
    client, _ = await client_for(licensed=True)

    usage = await _post(client, stream=True)

    assert usage["cache_read_tokens"] == 0
    assert usage["cached_tokens"] == 0
    assert usage["prompt_tokens_details"] == {"cached_tokens": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_unlicensed_route_reports_nothing_on_either_surface(client_for, stream):
    """The control: same upstream bytes, no declaration, so the miss stays unknown."""
    client, _ = await client_for(licensed=False)

    usage = await _post(client, stream=stream)

    assert "cache_read_tokens" not in usage
    assert "cached_tokens" not in usage
    assert "prompt_tokens_details" not in usage
