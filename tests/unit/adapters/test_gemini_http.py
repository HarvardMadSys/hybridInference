"""The Gemini adapter against a real local server: tool schemas, replies and streams.

Gemini rejects a tool schema it does not understand (an array without
``items`` is a 400), reports STOP for a finished tool call, and returns every
parallel function call complete in one part. The adapter has to normalize the
schema, give each call its own id (and, streaming, its own index), say
``tool_calls`` without hiding a truncation, and survive frames that carry no
candidate. None of the reply handling had a test; each one here goes over HTTP
to a local stand-in.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp import web

from serving.adapters.base import ModelConfig
from serving.adapters.gemini import GeminiAdapter
from serving.http import AsyncHTTPClient

pytestmark = pytest.mark.asyncio

USER = [{"role": "user", "content": "hi"}]


class Upstream:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.reply: Any = {}
        self.status = 200
        self.lines: list[str] | None = None
        self.stream_type = "text/event-stream"

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append(
            {
                "path": request.path,
                "query": dict(request.query),
                "headers": dict(request.headers),
                "json": await request.json(),
            }
        )
        if self.status >= 400:
            return web.Response(status=self.status, text=json.dumps(self.reply))
        if self.lines is not None:
            separator = "\n\n" if self.stream_type == "text/event-stream" else "\n"
            return web.Response(
                text="".join(f"{line}{separator}" for line in self.lines),
                content_type=self.stream_type,
            )
        return web.json_response(self.reply)

    @property
    def sent(self) -> dict[str, Any]:
        [request] = self.requests
        return request["json"]


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
    server.base_url = f"http://127.0.0.1:{port}/v1beta"
    try:
        yield server
    finally:
        await client.close()
        await runner.cleanup()


def _adapter(upstream: Upstream, **overrides: Any) -> GeminiAdapter:
    fields: dict[str, Any] = {
        "id": "gemini-test",
        "name": "Gemini Test",
        "provider": "gemini",
        "base_url": upstream.base_url,
        "api_key": "test-upstream-credential",
        "supports_tools": True,
    }
    fields.update(overrides)
    return GeminiAdapter(ModelConfig(**fields))


def _candidate(parts: list[dict[str, Any]], finish: str = "STOP") -> dict[str, Any]:
    return {"content": {"role": "model", "parts": parts}, "finishReason": finish}


def _call(name: str, **args: Any) -> dict[str, Any]:
    return {"functionCall": {"name": name, "args": args}}


# --- tool schemas ------------------------------------------------------------------------


def _declared(adapter: GeminiAdapter, parameters: Any) -> dict[str, Any]:
    tools = adapter._convert_tools(
        [{"type": "function", "function": {"name": "f", "parameters": parameters}}]
    )
    return tools[0]["functionDeclarations"][0]["parameters"]


async def test_an_array_schema_always_gets_items(upstream):
    """Gemini answers 400 INVALID_ARGUMENT to an array without ``items``."""
    schema = _declared(
        _adapter(upstream),
        {
            "type": "object",
            "properties": {
                "tags": {"type": "array"},
                "matrix": {"type": "array", "items": {"type": "array"}, "maxItems": 3},
            },
        },
    )

    assert schema["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}
    assert schema["properties"]["matrix"] == {
        "type": "array",
        "items": {"type": "array", "items": {"type": "string"}},
        "maxItems": 3,
    }


async def test_a_schema_keeps_what_gemini_accepts_and_drops_the_rest(upstream):
    schema = _declared(
        _adapter(upstream),
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "additionalProperties": False,
            "required": ["unit"],
            "properties": {
                "unit": {
                    "type": "string",
                    "enum": ["C", "F"],
                    "default": "C",
                    "description": "scale",
                },
                "days": {"type": ["null", "integer"], "minimum": 1, "maximum": 14},
                "when": {"type": "string", "pattern": "^\\d{4}$", "format": "date"},
                "anything": {},
                "odd": "not a schema",
                "nested": {"type": "object", "properties": {"x": {"type": "number"}}},
            },
        },
    )

    assert schema == {
        "type": "object",
        "required": ["unit"],
        "properties": {
            "unit": {"type": "string", "enum": ["C", "F"], "default": "C", "description": "scale"},
            "days": {"type": "integer", "minimum": 1, "maximum": 14},  # null dropped from union
            "when": {"type": "string", "pattern": "^\\d{4}$"},
            "anything": {"type": "string"},
            "odd": {"type": "string"},
            "nested": {"type": "object", "properties": {"x": {"type": "number"}}},
        },
    }


async def test_only_named_functions_are_declared(upstream):
    tools = _adapter(upstream)._convert_tools(
        [
            {"type": "function", "function": {"name": "a"}},
            {"type": "function", "function": {"description": "nameless"}},
            {"type": "function"},
            {"type": "retrieval"},
        ]
    )

    assert tools == [{"functionDeclarations": [{"name": "a", "description": ""}]}]
    assert _adapter(upstream)._convert_tools([{"type": "retrieval"}]) == []


@pytest.mark.parametrize(
    ("content", "text"),
    [
        (["a", {"type": "text", "text": "b"}, {"type": "image_url", "image_url": {}}], "a\nb"),
        ({"text": "only"}, "only"),
        ({"rows": 2}, '{"rows": 2}'),
        (42, "42"),
    ],
    ids=["blocks", "text-object", "other-object", "number"],
)
async def test_message_content_is_flattened_to_text(upstream, content, text):
    body = _adapter(upstream)._convert_messages_to_gemini([{"role": "user", "content": content}])

    assert body["contents"] == [{"role": "user", "parts": [{"text": text}]}]


# --- the request ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "method"), [("chat", "generateContent"), ("stream", "streamGenerateContent")]
)
async def test_the_request_names_the_model_and_carries_config_and_tools(upstream, mode, method):
    adapter = _adapter(upstream, provider_model_id="gemini-2.5-pro-001")
    params = {
        "max_tokens": 64,
        "temperature": 0.2,
        "top_k": 0,  # out of range: left out
        "stop": "END",
        "response_format": {"type": "json_object"},
        "tools": [{"type": "function", "function": {"name": "f"}}],
    }
    if mode == "chat":
        upstream.reply = {"candidates": [_candidate([{"text": "{}"}])]}
        await adapter.chat_completion(USER, **params)
    else:
        upstream.lines = [f"data: {json.dumps({'candidates': [_candidate([{'text': '{}'}])]})}"]
        [c async for c in adapter.stream_chat_completion(USER, **params)]

    [request] = upstream.requests
    assert request["path"] == f"/v1beta/models/gemini-2.5-pro-001:{method}"
    # In a header, not the URL: aiohttp renders the URL into every error it raises.
    assert "key" not in request["query"]
    assert request["headers"]["x-goog-api-key"] == "test-upstream-credential"
    assert (request["query"].get("alt") == "sse") is (mode == "stream")
    sent = request["json"]
    assert sent["generationConfig"] == {
        "maxOutputTokens": 64,
        "temperature": 0.2,
        "stopSequences": ["END"],
        "responseMimeType": "application/json",
    }
    assert sent["tools"] == [{"functionDeclarations": [{"name": "f", "description": ""}]}]


# --- the non-streaming reply ----------------------------------------------------------------


async def test_parallel_function_calls_get_distinct_ids_and_say_tool_calls(upstream):
    upstream.reply = {
        "candidates": [
            _candidate([{"text": "Checking."}, _call("weather", city="Paris"), _call("weather")])
        ],
        "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 4, "totalTokenCount": 11},
    }

    out = await _adapter(upstream).chat_completion(USER)

    choice = out["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "Checking."
    calls = choice["message"]["tool_calls"]
    assert [json.loads(c["function"]["arguments"]) for c in calls] == [{"city": "Paris"}, {}]
    assert len({c["id"] for c in calls}) == 2  # a tool result must find its own call


@pytest.mark.parametrize(
    ("finish", "with_call", "expected"),
    [
        ("MAX_TOKENS", True, "length"),  # a truncated call must not look complete
        ("SAFETY", False, "content_filter"),
        ("OTHER", False, "stop"),
        ("RECITATION", False, "stop"),
    ],
)
async def test_the_finish_reason_is_mapped(upstream, finish, with_call, expected):
    parts = [_call("f")] if with_call else [{"text": "x"}]
    upstream.reply = {"candidates": [_candidate(parts, finish)]}

    out = await _adapter(upstream).chat_completion(USER)

    assert out["choices"][0]["finish_reason"] == expected


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ({"promptFeedback": {"blockReason": "SAFETY"}}, "Unexpected Gemini response structure"),
        ({"candidates": []}, "Unexpected Gemini response structure"),
        ({"candidates": [{"finishReason": "MAX_TOKENS"}]}, "No content in candidate"),
    ],
    ids=["blocked-prompt", "no-candidates", "no-content"],
)
async def test_a_reply_without_an_answer_raises(upstream, reply, message):
    upstream.reply = reply

    with pytest.raises(ValueError, match=message):
        await _adapter(upstream).chat_completion(USER)


async def test_a_reply_with_no_parts_is_an_empty_answer(upstream):
    """The model spent its budget thinking: empty text, not an error."""
    upstream.reply = {
        "candidates": [{"content": {"role": "model"}, "finishReason": "MAX_TOKENS"}],
        "usageMetadata": {"promptTokenCount": 5, "totalTokenCount": 5},
    }

    out = await _adapter(upstream).chat_completion(USER)

    assert out["choices"][0]["message"]["content"] == ""
    assert out["choices"][0]["finish_reason"] == "length"
    assert out["usage"]["completion_tokens"] == 0


# --- the stream --------------------------------------------------------------------------------


def _frame(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}"


async def _stream(upstream: Upstream, lines: list[str], **params: Any) -> list[dict[str, Any]]:
    upstream.lines = lines
    chunks = [c async for c in _adapter(upstream).stream_chat_completion(USER, **params)]
    assert chunks[-1] == "data: [DONE]\n\n"
    return [json.loads(c[len("data: ") :]) for c in chunks[:-1]]


async def test_streamed_parallel_calls_get_increasing_indices_and_distinct_ids(upstream):
    """Clients merge streamed tool calls by index: a shared index corrupts both."""
    chunks = await _stream(
        upstream,
        [
            _frame({"candidates": [_candidate([_call("a", n=1), _call("b")], finish="")]}),
            _frame(
                {
                    "candidates": [_candidate([_call("c")])],
                    "usageMetadata": {
                        "promptTokenCount": 9,
                        "candidatesTokenCount": 3,
                        "totalTokenCount": 20,
                        "thoughtsTokenCount": 8,
                    },
                }
            ),
        ],
    )

    calls = [tc for c in chunks for tc in c["choices"][0]["delta"].get("tool_calls", [])]
    assert [(tc["index"], tc["function"]["name"]) for tc in calls] == [(0, "a"), (1, "b"), (2, "c")]
    assert json.loads(calls[0]["function"]["arguments"]) == {"n": 1}
    assert len({tc["id"] for tc in calls}) == 3
    final = chunks[-1]
    assert final["choices"][0]["finish_reason"] == "tool_calls"
    assert final["usage"] == {
        "prompt_tokens": 9,
        "completion_tokens": 3,
        "total_tokens": 20,
        "reasoning_tokens": 8,
    }


async def test_a_streamed_call_cut_off_at_max_tokens_stays_length(upstream):
    chunks = await _stream(
        upstream, [_frame({"candidates": [_candidate([_call("a")], "MAX_TOKENS")]})]
    )

    assert chunks[-1]["choices"][0]["finish_reason"] == "length"


async def test_the_stream_survives_odd_frames_and_stops_at_done(upstream):
    chunks = await _stream(
        upstream,
        [
            _frame({"candidates": [_candidate([{"text": "Hel"}], finish="")]}),
            "data: {not json",
            _frame({"deltaText": "lo"}),  # text at the top level, outside any candidate
            _frame({"candidates": [{"finishReason": "STOP"}]}),  # finish without content
            "data: [DONE]",
            _frame({"candidates": [_candidate([{"text": "after done"}])]}),
        ],
    )

    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert text == "Hello"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


async def test_without_usage_metadata_the_stream_estimates_usage(upstream):
    chunks = await _stream(
        upstream, [_frame({"candidates": [_candidate([{"text": "four words of text"}])]})]
    )

    usage = chunks[-1]["usage"]
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert "reasoning_tokens" not in usage and "cache_read_tokens" not in usage


async def test_a_stream_refused_upfront_raises(upstream):
    upstream.status = 429
    upstream.reply = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}

    with pytest.raises(Exception) as caught:
        [c async for c in _adapter(upstream).stream_chat_completion(USER)]

    assert "429" in str(caught.value)


@pytest.mark.parametrize("mode", ["chat", "stream"])
async def test_an_upstream_error_does_not_carry_the_credential(upstream, mode):
    # Its text reaches logs and the request log's metadata (``str(exc)``).
    upstream.status = 429
    upstream.reply = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}
    adapter = _adapter(upstream)

    with pytest.raises(Exception) as caught:
        if mode == "chat":
            await adapter.chat_completion(USER)
        else:
            [c async for c in adapter.stream_chat_completion(USER)]

    assert "test-upstream-credential" not in str(caught.value)


async def test_an_ndjson_stream_is_read_and_stops_at_done(upstream):
    """Vertex can answer with newline-delimited JSON instead of SSE."""
    upstream.stream_type = "application/x-ndjson"
    frames = [
        json.dumps({"candidates": [_candidate([{"text": "Hel"}], finish="")]}),
        json.dumps({"candidates": [_candidate([{"text": "lo"}])]}),
        "[DONE]",
        json.dumps({"candidates": [_candidate([{"text": " after done"}])]}),
    ]

    chunks = await _stream(upstream, frames)

    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert text == "Hello"
