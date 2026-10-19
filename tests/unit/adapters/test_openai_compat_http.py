"""The OpenAI-compatible adapter against a real local server.

This adapter serves local vLLM/SGLang/Ollama and most remote providers, and
several of its request and stream paths had no test: the URL a configured
``chat_path`` / ``extra_query`` produces, providers that authenticate with a
custom header, tools and structured output on the wire, a provider still
streaming the legacy ``function_call`` delta, a key whose outbound slot is
full, and a stream that times out or never sends anything. Each one here goes
over HTTP to a local stand-in.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.adapters.upstream_limiter import UpstreamSaturated
from serving.exceptions import UpstreamStreamIdleError
from serving.http import AsyncHTTPClient

pytestmark = pytest.mark.asyncio

USER = [{"role": "user", "content": "hi"}]
TOOLS = [
    {
        "type": "function",
        "function": {"name": "weather", "parameters": {"type": "object", "properties": {}}},
    }
]


class Upstream:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.frames: list[dict[str, Any] | str] | None = None
        self.first_byte_delay = 0.0
        self.gap_after_first = 0.0
        self.reply: dict[str, Any] = {
            "id": "c1",
            "object": "chat.completion",
            "created": 1,
            "model": "upstream-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(
            {
                "path": request.path,
                "query": dict(request.query),
                "headers": {k.lower(): v for k, v in request.headers.items()},
                "json": await request.json(),
            }
        )
        if self.frames is None:
            return web.json_response(self.reply)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        if self.first_byte_delay:
            await asyncio.sleep(self.first_byte_delay)
        for i, frame in enumerate(self.frames):
            if i == 1 and self.gap_after_first:
                await asyncio.sleep(self.gap_after_first)
            data = frame if isinstance(frame, str) else json.dumps(frame)
            await response.write(f"data: {data}\n\n".encode())
        await response.write_eof()
        return response

    @property
    def sent(self) -> dict[str, Any]:
        return self.requests[-1]["json"]


@pytest.fixture
async def upstream(monkeypatch):
    client = AsyncHTTPClient()  # one session per test, on this test's loop
    monkeypatch.setattr(AsyncHTTPClient, "_shared", client)
    server = Upstream()
    app = web.Application()
    app.router.add_route("POST", "/{tail:.*}", server.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    server.base_url = f"http://127.0.0.1:{port}"
    try:
        yield server
    finally:
        await client.close()
        await runner.cleanup()


def _adapter(upstream: Upstream, **overrides: Any) -> OpenAICompatAdapter:
    fields: dict[str, Any] = {
        "id": "local-model",
        "name": "Local Model",
        "provider": "vllm",
        "base_url": f"{upstream.base_url}/v1",
        "provider_model_id": "upstream-model",
        "api_key": "test-upstream-credential",
        "supports_tools": True,
        "supports_structured_output": True,
        "supported_params": ["temperature", "max_tokens"],
    }
    fields.update(overrides)
    return OpenAICompatAdapter(ModelConfig(**fields))


def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "upstream-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


async def _stream(adapter: OpenAICompatAdapter, **params: Any) -> list[str]:
    return [c async for c in adapter.stream_chat_completion(USER, **params)]


def _data(chunks: list[str]) -> list[dict[str, Any]]:
    return [
        json.loads(c[len("data: ") :])
        for c in chunks
        if c.startswith("data: ") and "[DONE]" not in c
    ]


# --- where the request goes, and how it authenticates --------------------------------


@pytest.mark.parametrize(
    ("base", "chat_path", "extra_query", "path", "query"),
    [
        ("/v1", None, None, "/v1/chat/completions", {}),
        ("", None, None, "/v1/chat/completions", {}),
        ("/openai", "chat", None, "/openai/chat", {}),  # a path without its slash
        (
            "/v1",
            None,
            {"api-version": "2024-10-21"},
            "/v1/chat/completions",
            {"api-version": "2024-10-21"},
        ),
    ],
    ids=["v1-base", "bare-host", "chat-path-no-slash", "extra-query"],
)
async def test_the_request_url(upstream, base, chat_path, extra_query, path, query):
    adapter = _adapter(
        upstream,
        base_url=f"{upstream.base_url}{base}",
        chat_path=chat_path,
        extra_query=extra_query,
    )

    await adapter.chat_completion(USER)

    [request] = upstream.requests
    assert request["path"] == path
    assert request["query"] == query


async def test_a_custom_auth_header_and_extra_headers(upstream):
    adapter = _adapter(
        upstream,
        use_bearer_auth=False,
        auth_header_name="api-key",
        auth_format="{api_key}",
        extra_headers={"X-Org": "acme"},
    )

    await adapter.chat_completion(USER)

    headers = upstream.requests[0]["headers"]
    assert headers["api-key"] == "test-upstream-credential"
    assert "authorization" not in headers
    assert headers["x-org"] == "acme"


# --- what the request carries ---------------------------------------------------------


@pytest.mark.parametrize("mode", ["chat", "stream"])
async def test_tools_and_tool_choice_reach_the_upstream(upstream, mode):
    adapter = _adapter(upstream)
    params = {"tools": TOOLS, "tool_choice": "required"}
    if mode == "chat":
        await adapter.chat_completion(USER, **params)
    else:
        upstream.frames = [_chunk({"content": "ok"}, "stop"), "[DONE]"]
        await _stream(adapter, **params)

    assert upstream.sent["tools"][0]["function"]["name"] == "weather"
    assert upstream.sent["tool_choice"] == "required"


@pytest.mark.parametrize("mode", ["chat", "stream"])
async def test_a_model_without_tool_support_is_sent_no_tools(upstream, mode):
    adapter = _adapter(upstream, supports_tools=False)
    params = {"tools": TOOLS, "tool_choice": "required"}
    if mode == "chat":
        await adapter.chat_completion(USER, **params)
    else:
        upstream.frames = [_chunk({"content": "ok"}, "stop"), "[DONE]"]
        await _stream(adapter, **params)

    assert "tools" not in upstream.sent
    assert "tool_choice" not in upstream.sent


SCHEMA = {"type": "object", "properties": {"x": {"type": "integer"}}}
JSON_SCHEMA_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "x", "schema": SCHEMA},
    "schema": SCHEMA,
}


@pytest.mark.parametrize(
    ("profile", "response_format", "sent_format", "guided"),
    [
        (None, JSON_SCHEMA_FORMAT, JSON_SCHEMA_FORMAT, True),
        ("deepseek", {"type": "json_object"}, {"type": "json_object"}, False),
        ("deepseek", JSON_SCHEMA_FORMAT, None, False),  # DeepSeek takes json_object only
        ("kimi", JSON_SCHEMA_FORMAT, JSON_SCHEMA_FORMAT, False),  # schema yes, guided_json no
    ],
    ids=["vllm-schema", "deepseek-object", "deepseek-schema", "kimi-schema"],
)
async def test_a_streamed_structured_output_request(
    upstream, profile, response_format, sent_format, guided
):
    """vLLM also takes ``guided_json``; DeepSeek's API would reject the field."""
    adapter = _adapter(upstream, provider_profile=profile)
    upstream.frames = [_chunk({"content": "{}"}, "stop"), "[DONE]"]

    await _stream(adapter, response_format=response_format)

    assert upstream.sent.get("response_format") == sent_format
    assert ("guided_json" in upstream.sent) is guided
    if guided:
        assert upstream.sent["guided_json"] == SCHEMA


async def test_structured_output_is_not_sent_to_a_model_without_it(upstream):
    adapter = _adapter(upstream, supports_structured_output=False)
    upstream.frames = [_chunk({"content": "{}"}, "stop"), "[DONE]"]

    await _stream(adapter, response_format=JSON_SCHEMA_FORMAT)

    assert "response_format" not in upstream.sent
    assert "guided_json" not in upstream.sent


# --- what comes back ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("timer", "idle_env", "socket_env", "fired_after"),
    [
        # Only the per-frame detector is armed; the socket would wait forever.
        ("per-frame", "0.3", None, 0.3),
        # The detector is off; the socket's own read timeout must be converted.
        ("socket", "0", "0.2", 0.2),
    ],
    ids=["per-frame-detector", "socket-read-timeout"],
)
async def test_a_stream_that_stalls_after_its_first_frame_is_an_error(
    upstream, monkeypatch, timer, idle_env, socket_env, fired_after
):
    """A stall mid-stream must not end as a normal [DONE] on a truncated answer.

    Each case arms exactly one timer, and ``idle_seconds`` says which one fired.
    """
    monkeypatch.setenv("STREAM_IDLE_TIMEOUT_SECONDS", idle_env)
    if socket_env is None:
        monkeypatch.delenv("STREAM_FIRST_BYTE_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("STREAM_FIRST_BYTE_TIMEOUT_SECONDS", socket_env)
    upstream.gap_after_first = 1.5
    upstream.frames = [
        _chunk({"content": "partial"}),
        _chunk({"content": " rest"}, "stop"),
        "[DONE]",
    ]
    seen: list[str] = []

    with pytest.raises(UpstreamStreamIdleError) as caught:
        async for chunk in _adapter(upstream).stream_chat_completion(USER):
            seen.append(chunk)

    assert caught.value.idle_seconds == pytest.approx(fired_after)
    assert any("partial" in c for c in seen)
    assert not any("[DONE]" in c for c in seen)


async def test_a_stream_that_never_sends_its_first_byte_in_time_fails_before_any_output(
    upstream, monkeypatch
):
    """Before the first byte the error escapes unchanged, so the router can still fall back."""
    monkeypatch.setenv("STREAM_FIRST_BYTE_TIMEOUT_SECONDS", "0.2")
    upstream.first_byte_delay = 1.0
    upstream.frames = [_chunk({"content": "late"}, "stop"), "[DONE]"]
    seen: list[str] = []

    with pytest.raises(asyncio.TimeoutError):
        async for chunk in _adapter(upstream).stream_chat_completion(USER):
            seen.append(chunk)

    assert seen == []


async def test_an_empty_stream_is_an_error_not_an_empty_answer(upstream):
    upstream.frames = []

    with pytest.raises(aiohttp.ClientError, match="without a terminal finish_reason"):
        await _stream(_adapter(upstream))


async def test_a_stream_cut_before_its_finish_is_an_error(upstream):
    upstream.frames = [_chunk({"content": "half an ans"})]  # no finish_reason, no [DONE]

    with pytest.raises(aiohttp.ClientError, match="without a terminal finish_reason"):
        await _stream(_adapter(upstream))


# --- key pool rotation past a full outbound slot ---------------------------------------------


@pytest.mark.parametrize("mode", ["chat", "stream"])
async def test_a_key_whose_slot_is_full_is_skipped_not_muted(upstream, monkeypatch, mode):
    adapter = _adapter(upstream, api_key=None, api_keys=["sk-test-first-key", "sk-test-second-key"])
    real_acquire = adapter._acquire_upstream_slot

    async def acquire(api_key):
        if api_key == "sk-test-first-key":
            raise UpstreamSaturated("first key's allowance is full")
        return await real_acquire(api_key)

    monkeypatch.setattr(adapter, "_acquire_upstream_slot", acquire)

    if mode == "chat":
        await adapter.chat_completion(USER)
    else:
        upstream.frames = [_chunk({"content": "ok"}, "stop"), "[DONE]"]
        await _stream(adapter)

    [request] = upstream.requests  # the first key never reached the upstream
    assert request["headers"]["authorization"] == "Bearer sk-test-second-key"
    first = next(s for s in adapter._key_pool._keys if s.key == "sk-test-first-key")
    assert first.cooldown_until == 0.0  # nothing was sent with it, so it is not muted


@pytest.mark.parametrize("mode", ["chat", "stream"])
async def test_when_every_key_is_full_the_saturation_propagates(upstream, monkeypatch, mode):
    adapter = _adapter(upstream, api_key=None, api_keys=["sk-test-first-key", "sk-test-second-key"])

    async def full(api_key):
        raise UpstreamSaturated("no allowance left")

    monkeypatch.setattr(adapter, "_acquire_upstream_slot", full)

    with pytest.raises(UpstreamSaturated):
        if mode == "chat":
            await adapter.chat_completion(USER)
        else:
            upstream.frames = [_chunk({"content": "ok"}, "stop"), "[DONE]"]
            await _stream(adapter)

    assert upstream.requests == []
