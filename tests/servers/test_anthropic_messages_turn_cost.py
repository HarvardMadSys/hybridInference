"""The per-turn work of a streamed /v1/messages request stays bounded.

Every streamed turn seeds message_start.usage.input_tokens with a token estimate
(computed on the event loop) and snapshots the client's body for the log. Both
used to scale with the whole conversation: a full tiktoken encode and two or
three deep copies per turn.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from serving.adapters.anthropic_translator import anthropic_request_to_openai
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.routers import anthropic_messages
from serving.utils import tokens
from serving.utils.tokens import INLINE_ESTIMATE_MAX_CHARS, estimate_prompt_tokens
from tests.servers.conftest import ANTHROPIC_TEST_API_KEY

AUTH = {"Authorization": f"Bearer {ANTHROPIC_TEST_API_KEY}"}
OPENAI_MODEL = "glm-4.7"


class _Upstream(BaseAdapter):
    """An OpenAI-backed adapter whose stream reports no usage."""

    async def chat_completion(self, messages, **params):  # pragma: no cover - unused
        raise NotImplementedError

    async def stream_chat_completion(self, messages, **params):
        delta = {"choices": [{"index": 0, "delta": {"content": "ok"}}]}
        yield f"data: {json.dumps(delta)}\n\n"


def _body(chars: int) -> dict[str, Any]:
    turns = []
    size = 0
    index = 0
    while size < chars:
        text = f"turn {index}: " + "def handler(request): return request.body  # " * 60
        turns.append({"role": "user" if index % 2 == 0 else "assistant", "content": text})
        size += len(text)
        index += 1
    if turns[-1]["role"] != "user":
        turns.append({"role": "user", "content": "continue"})
    return {"model": "m", "max_tokens": 64, "system": "You are terse.", "messages": turns}


async def _seed(body: dict[str, Any]) -> int:
    adapter = _Upstream(ModelConfig(id="m", name="m", provider="test", base_url="http://t"))
    async for frame in adapter.stream_messages(body, request_id="r"):
        if frame.startswith(b"event: message_start"):
            payload = frame.split(b"data: ", 1)[1]
            return json.loads(payload)["message"]["usage"]["input_tokens"]
    raise AssertionError("no message_start")


@pytest.mark.asyncio
async def test_a_short_turn_is_seeded_with_the_exact_estimate() -> None:
    body = _body(2_000)
    oai_messages, _ = anthropic_request_to_openai(body)
    assert await _seed(body) == estimate_prompt_tokens(oai_messages)


@pytest.mark.asyncio
async def test_the_seed_counts_the_tool_definitions() -> None:
    body = _body(2_000)
    body["tools"] = [
        {"name": f"tool_{i}", "description": "Edits files. " * 50, "input_schema": {}}
        for i in range(5)
    ]
    oai_messages, oai_params = anthropic_request_to_openai(copy.deepcopy(body))
    # As count_tokens counts it: the messages plus the serialized tool schemas.
    expected = estimate_prompt_tokens(oai_messages) + tokens.estimate_text_tokens(
        json.dumps(oai_params["tools"])
    )
    sink: dict[str, Any] = {}
    adapter = _Upstream(ModelConfig(id="m", name="m", provider="test", base_url="http://t"))
    seed = None
    async for frame in adapter.stream_messages(body, request_id="r", usage_sink=sink):
        if frame.startswith(b"event: message_start"):
            seed = json.loads(frame.split(b"data: ", 1)[1])["message"]["usage"]["input_tokens"]
    assert seed == expected
    # Declared as the guess it is, for the router's cancel billing.
    assert sink[anthropic_messages.INPUT_SEED_SINK_KEY] == seed


@pytest.mark.asyncio
async def test_a_long_turn_is_seeded_from_a_bounded_sample(monkeypatch) -> None:
    body = _body(1_500_000)
    oai_messages, _ = anthropic_request_to_openai(body)
    exact = estimate_prompt_tokens(oai_messages)

    tokenized: list[int] = []
    count = tokens._count_with_tiktoken

    def spy(text: str) -> int:
        tokenized.append(len(text))
        return count(text)

    monkeypatch.setattr(tokens, "_count_with_tiktoken", spy)
    started = time.perf_counter()
    seed = await _seed(body)
    elapsed = time.perf_counter() - started

    assert abs(seed - exact) <= 0.05 * exact
    # 1.5M characters of conversation; only about the budget was tokenized.
    assert sum(tokenized) <= INLINE_ESTIMATE_MAX_CHARS + 2 * 512
    assert elapsed < 0.5


def test_the_teardown_estimate_is_exact_for_a_short_prompt() -> None:
    tools = [{"name": "Read", "input_schema": {"type": "object"}}]
    payload = {
        "system": "You are terse.",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": tools,
    }
    expected = estimate_prompt_tokens(
        [{"role": "system", "content": "You are terse."}, {"role": "user", "content": "hi"}]
    ) + tokens.estimate_text_tokens(json.dumps(tools))
    assert anthropic_messages._estimate_request_input_tokens(payload) == expected


def test_the_teardown_estimate_is_bounded_for_a_long_prompt(monkeypatch) -> None:
    payload = _body(1_500_000)
    exact = estimate_prompt_tokens(
        [{"role": "system", "content": payload["system"]}, *payload["messages"]]
    )
    tokenized: list[int] = []
    count = tokens._count_with_tiktoken

    def spy(text: str) -> int:
        tokenized.append(len(text))
        return count(text)

    monkeypatch.setattr(tokens, "_count_with_tiktoken", spy)
    estimate = anthropic_messages._estimate_request_input_tokens(payload)
    assert abs(estimate - exact) <= 0.05 * exact
    assert sum(tokenized) <= INLINE_ESTIMATE_MAX_CHARS + 2 * 512


@pytest.mark.asyncio
async def test_a_streamed_turn_snapshots_the_body_once(
    anthropic_test_client, mock_log_store, monkeypatch
) -> None:
    openai_sse = (
        b'data: {"id":"x","object":"chat.completion.chunk","model":"glm-4.7",'
        b'"choices":[{"index":0,"delta":{"content":"Hi"}}]}\n\n'
        b'data: {"id":"x","object":"chat.completion.chunk","model":"glm-4.7",'
        b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}\n\n'
    )

    class _Content:
        async def iter_any(self):
            yield openai_sse

        async def iter_chunked(self, n):
            yield openai_sse

    class _Response:
        status = 200
        content = _Content()
        headers: dict = {"Content-Type": "text/event-stream"}  # noqa: RUF012

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    sent: dict[str, Any] = {}

    class _Session:
        def post(self, url, json=None, headers=None, timeout=None):
            sent["json"] = json
            return _Response()

    async def fake_ensure_session(self):
        return _Session()

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", fake_ensure_session)
    copies: list[Any] = []

    def counting_deepcopy(value: Any, *args: Any) -> Any:
        copies.append(value)
        return copy.deepcopy(value, *args)

    monkeypatch.setattr(anthropic_messages, "copy", SimpleNamespace(deepcopy=counting_deepcopy))

    cached = {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
    body = {
        "model": OPENAI_MODEL,
        "max_tokens": 50,
        "stream": True,
        "messages": [{"role": "user", "content": [cached]}],
    }
    async with anthropic_test_client.stream(
        "POST", "/v1/messages", json=body, headers=AUTH
    ) as response:
        assert response.status_code == 200
        async for _ in response.aiter_bytes():
            pass

    assert len(copies) == 1
    deadline = time.monotonic() + 2
    while not mock_log_store.log_request.call_count:
        assert time.monotonic() < deadline
        await asyncio.sleep(0.02)
    row = mock_log_store.log_request.call_args.kwargs
    # The log keeps what the client sent, from that one snapshot ...
    assert row["prompt"] == body["messages"]
    assert row["prompt"] is row["request_payload"]["messages"]
    # ... while the OpenAI-backed upstream got the sanitized copy.
    assert "cache_control" not in json.dumps(sent["json"]["messages"])
