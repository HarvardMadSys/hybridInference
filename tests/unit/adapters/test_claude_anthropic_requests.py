"""What the Claude (Vertex) and Anthropic adapters send, and what they make of the reply.

Both adapters build a Messages-API request from OpenAI-shaped parameters on
four paths (chat and stream for each). Only the bare minimum of that had a
test: nothing showed that ``stop`` becomes ``stop_sequences``, that a system
parameter wins over system messages, that ``top_k`` is passed only to a model
that declares it, or that tools are withheld from a model without tool
support. Each path here talks to a real local HTTP server, so the request is
the one that actually goes over the wire.
"""

from __future__ import annotations

import json
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from serving.adapters.anthropic import AnthropicAdapter
from serving.adapters.base import ModelConfig
from serving.adapters.claude import ClaudeAdapter
from serving.http import AsyncHTTPClient

pytestmark = pytest.mark.asyncio


class Upstream:
    """A local Messages-API stand-in that records every request it gets."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.reply: Any = {}
        self.status = 200
        self.sse: list[dict[str, Any]] | None = None

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(
            {"path": request.path, "headers": dict(request.headers), "json": await request.json()}
        )
        if self.status >= 400:
            return web.Response(status=self.status, text=json.dumps(self.reply))
        if self.sse is not None:
            body = "".join(
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in self.sse
            )
            return web.Response(text=body, content_type="text/event-stream")
        return web.json_response(self.reply)

    @property
    def sent(self) -> dict[str, Any]:
        [request] = self.requests
        return request["json"]


@pytest.fixture
async def upstream(monkeypatch):
    # The shared client keeps one session for the process; give each test its
    # own, bound to this test's event loop.
    client = AsyncHTTPClient()
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


def _config(kind: str, base_url: str, **overrides: Any) -> ModelConfig:
    fields: dict[str, Any] = {
        "id": "claude-test",
        "name": "Claude Test",
        "provider": kind,
        "base_url": base_url,
        "api_key": "test-upstream-credential",
        "max_output_length": 1024,
        "supports_tools": True,
        "supported_params": ["temperature", "top_p", "top_k", "stop", "tools", "tool_choice"],
    }
    if kind == "anthropic":
        fields["provider_model_id"] = "claude-test-20990101"
    fields.update(overrides)
    return ModelConfig(**fields)


def _adapter(kind: str, upstream: Upstream, **overrides: Any):
    cls = ClaudeAdapter if kind == "claude" else AnthropicAdapter
    return cls(_config(kind, upstream.base_url, **overrides))


def _message_reply(**overrides: Any) -> dict[str, Any]:
    reply = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "output_tokens": 1},
    }
    reply.update(overrides)
    return reply


def _text_events(text: str = "ok") -> list[dict[str, Any]]:
    return [
        {"type": "message_start", "message": {"usage": {"input_tokens": 3, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]


async def _send(kind: str, mode: str, upstream: Upstream, messages, adapter=None, **params):
    """Run one request on ``mode`` ("chat" or "stream") and return what came back."""
    adapter = adapter or _adapter(kind, upstream)
    if mode == "chat":
        upstream.reply = _message_reply()
        return await adapter.chat_completion(messages, **params)
    upstream.sse = _text_events()
    return [chunk async for chunk in adapter.stream_chat_completion(messages, **params)]


PATHS = [
    pytest.param("claude", "chat", id="claude-chat"),
    pytest.param("claude", "stream", id="claude-stream"),
    pytest.param("anthropic", "chat", id="anthropic-chat"),
    pytest.param("anthropic", "stream", id="anthropic-stream"),
]

USER = [{"role": "user", "content": "hi"}]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "weather",
            "description": "look it up",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
]


# --- the request ---------------------------------------------------------------------


@pytest.mark.parametrize(("kind", "mode"), PATHS)
async def test_sampling_parameters_reach_the_upstream_clamped(upstream, kind, mode):
    await _send(
        kind, mode, upstream, USER, max_tokens=99_999, temperature=3.5, top_p=1.5, stop=["END"]
    )

    sent = upstream.sent
    assert sent["max_tokens"] == 1024  # the model's output limit
    assert sent["temperature"] == 2.0
    assert sent["top_p"] == 1.0
    assert sent["stop_sequences"] == ["END"]  # Claude's name for ``stop``
    assert "stop" not in sent


@pytest.mark.parametrize(("kind", "mode"), PATHS)
async def test_without_parameters_only_the_output_limit_is_sent(upstream, kind, mode):
    await _send(kind, mode, upstream, USER)

    sent = upstream.sent
    assert sent["max_tokens"] == 1024
    for absent in ("temperature", "top_p", "top_k", "stop_sequences", "system", "tools"):
        assert absent not in sent


@pytest.mark.parametrize(("kind", "mode"), PATHS)
async def test_system_messages_become_the_system_prompt(upstream, kind, mode):
    await _send(
        kind,
        mode,
        upstream,
        [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "no emoji"},
        ],
    )

    assert upstream.sent["system"] == "be brief\n\nno emoji"
    assert [m["role"] for m in upstream.sent["messages"]] == ["user"]


@pytest.mark.parametrize(("kind", "mode"), PATHS)
async def test_a_system_parameter_wins_over_system_messages(upstream, kind, mode):
    await _send(
        kind,
        mode,
        upstream,
        [{"role": "system", "content": "from messages"}, {"role": "user", "content": "hi"}],
        system="from the parameter",
    )

    assert upstream.sent["system"] == "from the parameter"


@pytest.mark.parametrize(
    ("kind", "mode"),
    # Anthropic streaming is left out: it does not pass top_k on (fixed separately).
    [p for p in PATHS if p.id != "anthropic-stream"],
)
async def test_top_k_is_passed_only_to_a_model_that_declares_it(upstream, kind, mode):
    await _send(kind, mode, upstream, USER, top_k=40)
    declared = upstream.requests.pop()["json"]

    undeclared_adapter = _adapter(kind, upstream, supported_params=["temperature"])
    await _send(kind, mode, upstream, USER, adapter=undeclared_adapter, top_k=40)

    assert declared["top_k"] == 40
    assert "top_k" not in upstream.sent


@pytest.mark.parametrize(("kind", "mode"), PATHS)
async def test_tools_and_tool_choice_are_converted(upstream, kind, mode):
    await _send(
        kind,
        mode,
        upstream,
        USER,
        tools=TOOLS,
        tool_choice={"type": "function", "function": {"name": "weather"}},
    )

    sent = upstream.sent
    assert sent["tools"] == [
        {
            "name": "weather",
            "description": "look it up",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    ]
    assert sent["tool_choice"] == {"type": "tool", "name": "weather"}


@pytest.mark.parametrize(("kind", "mode"), PATHS)
async def test_tool_choice_none_keeps_tool_declarations(upstream, kind, mode):
    await _send(kind, mode, upstream, USER, tools=TOOLS, tool_choice="none")

    assert upstream.sent["tools"][0]["name"] == "weather"
    assert upstream.sent["tool_choice"] == {"type": "none"}


@pytest.mark.parametrize("mode", ["chat", "stream"])
async def test_vertex_none_with_tool_history_preserves_tools_and_history(upstream, mode):
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "weather", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "sunny"},
        {"role": "user", "content": "Now answer."},
    ]

    await _send("claude", mode, upstream, messages, tools=TOOLS, tool_choice="none")

    sent = upstream.sent
    assert sent["tool_choice"] == {"type": "none"}
    assert sent["tools"][0]["name"] == "weather"
    assert sent["messages"][0]["content"][0]["type"] == "tool_use"
    assert sent["messages"][1]["content"][0]["type"] == "tool_result"


@pytest.mark.parametrize("mode", ["chat", "stream"])
async def test_vertex_parallel_tool_calls_false_disables_parallel_tool_use(upstream, mode):
    await _send("claude", mode, upstream, USER, tools=TOOLS, parallel_tool_calls=False)

    assert upstream.sent["tool_choice"] == {
        "type": "auto",
        "disable_parallel_tool_use": True,
    }


@pytest.mark.parametrize(("kind", "mode"), PATHS)
async def test_a_model_without_tool_support_is_sent_no_tools(upstream, kind, mode):
    adapter = _adapter(kind, upstream, supports_tools=False)

    await _send(kind, mode, upstream, USER, adapter=adapter, tools=TOOLS, tool_choice="required")

    assert "tools" not in upstream.sent
    assert "tool_choice" not in upstream.sent


@pytest.mark.parametrize(
    ("mode", "path"), [("chat", "/v1:rawPredict"), ("stream", "/v1:streamRawPredict")]
)
async def test_claude_vertex_endpoint_and_credentials(upstream, mode, path):
    await _send("claude", mode, upstream, USER)

    [request] = upstream.requests
    assert request["path"] == path
    assert request["headers"]["api-key"] == "test-upstream-credential"
    sent = request["json"]
    assert sent["anthropic_version"] == ClaudeAdapter.ANTHROPIC_VERSION
    assert "model" not in sent  # Vertex names the model in the URL


@pytest.mark.parametrize("mode", ["chat", "stream"])
async def test_anthropic_endpoint_model_and_credentials(upstream, mode):
    await _send("anthropic", mode, upstream, USER)

    [request] = upstream.requests
    assert request["path"] == "/v1/messages"
    headers = {k.lower(): v for k, v in request["headers"].items()}
    assert headers["x-api-key"] == "test-upstream-credential"
    assert headers["anthropic-version"] == AnthropicAdapter.ANTHROPIC_VERSION
    assert request["json"]["model"] == "claude-test-20990101"  # the provider's own id
    assert request["json"].get("stream", False) is (mode == "stream")


# --- the reply ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["claude", "anthropic"])
async def test_a_tool_use_reply_becomes_openai_tool_calls(upstream, kind):
    upstream.reply = _message_reply(
        content=[
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "toolu_1", "name": "weather", "input": {"city": "Rome"}},
        ],
        stop_reason="tool_use",
        usage={
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 20,
        },
    )

    out = await _adapter(kind, upstream).chat_completion(USER)

    choice = out["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "Checking."
    [call] = choice["message"]["tool_calls"]
    assert (call["id"], call["function"]["name"]) == ("toolu_1", "weather")
    assert json.loads(call["function"]["arguments"]) == {"city": "Rome"}
    usage = out["usage"]
    assert usage["completion_tokens"] == 5
    assert usage["prompt_tokens_details"]["cached_tokens"] == 100


@pytest.mark.parametrize(
    ("stop_reason", "finish_reason"),
    [("max_tokens", "length"), ("stop_sequence", "stop"), ("end_turn", "stop")],
)
async def test_claude_maps_the_stop_reason(upstream, stop_reason, finish_reason):
    upstream.reply = _message_reply(stop_reason=stop_reason)

    out = await _adapter("claude", upstream).chat_completion(USER)

    assert out["choices"][0]["finish_reason"] == finish_reason


async def test_a_vertex_error_body_is_raised_not_returned_as_text(upstream):
    upstream.reply = {"Code": 429, "Error": "quota exhausted"}

    with pytest.raises(RuntimeError, match="quota exhausted"):
        await _adapter("claude", upstream).chat_completion(USER)


async def test_an_anthropic_stream_refused_upfront_raises_with_the_status(upstream):
    upstream.status = 429
    upstream.reply = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}

    with pytest.raises(aiohttp.ClientResponseError) as caught:
        async for _ in _adapter("anthropic", upstream).stream_chat_completion(USER):
            pass

    assert caught.value.status == 429
    assert "rate_limit_error" in caught.value.message


def _data_chunks(chunks: list[str]) -> list[dict[str, Any]]:
    return [
        json.loads(chunk[len("data: ") :])
        for chunk in chunks
        if chunk.startswith("data: ") and "[DONE]" not in chunk
    ]


async def test_an_anthropic_tool_stream_ends_with_tool_calls_and_cache_usage(upstream):
    upstream.sse = [
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 20,
                }
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "weather", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"city": "Ro'},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": 'me"}'},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 7},
        },
        {"type": "message_stop"},
    ]

    chunks = [c async for c in _adapter("anthropic", upstream).stream_chat_completion(USER)]

    data = _data_chunks(chunks)
    calls = [
        call
        for chunk in data
        for call in (chunk["choices"][0]["delta"].get("tool_calls") or [])
        if chunk.get("choices")
    ]
    assert [(c["id"], c["function"]["name"]) for c in calls] == [("toolu_1", "weather")]
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "Rome"}
    final = data[-1]
    assert final["choices"][0]["finish_reason"] == "tool_calls"
    # Prompt tokens include the cached ones; the split travels alongside.
    assert final["usage"] == {
        "prompt_tokens": 130,
        "completion_tokens": 7,
        "total_tokens": 137,
        "cache_read_tokens": 100,
        "cache_write_tokens": 20,
    }
    assert chunks[-1].strip() == "data: [DONE]"


def _tool_events(stop_reason: str | None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 3, "output_tokens": 0}}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "weather", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": "{}"},
        },
        {"type": "content_block_stop", "index": 0},
    ]
    if stop_reason is not None:
        events.append(
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason},
                "usage": {"output_tokens": 2},
            }
        )
        events.append({"type": "message_stop"})
    return events


@pytest.mark.parametrize(
    ("stop_reason", "finish_reason"),
    [
        ("end_turn", "tool_calls"),
        (None, "tool_calls"),  # the upstream closed before saying why
        ("max_tokens", "length"),  # a truncated call must not look complete
    ],
    ids=["end-turn", "no-stop-reason", "max-tokens"],
)
async def test_an_anthropic_stream_with_a_tool_call_says_so(upstream, stop_reason, finish_reason):
    upstream.sse = _tool_events(stop_reason)

    chunks = [c async for c in _adapter("anthropic", upstream).stream_chat_completion(USER)]

    assert _data_chunks(chunks)[-1]["choices"][0]["finish_reason"] == finish_reason
