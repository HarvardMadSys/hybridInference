"""/v1/messages logs and bills the upstream's exact reasoning count, as chat does.

Driven through the real route and the real OpenAI-compatible adapter, with only
the upstream HTTP faked. The upstream's reasoning count and reported total have
no Anthropic field, so they travel beside the response to the log row; the
client still receives plain Anthropic usage.

Shapes the upstream may report:

* not at all -- the row's ``reasoning_tokens`` stays NULL (unknown);
* an explicit 0 -- stored as 0, not NULL;
* DeepSeek/OpenAI style -- reasoning already inside ``completion_tokens``,
  proved by ``total = prompt + completion``: billed once;
* Gemini style -- reasoning outside ``completion_tokens``, ``total = prompt +
  completion + reasoning``: billed, and counted in Anthropic ``output_tokens``.

In every case the logged cost and the quota increment equal what the chat
surface charges for the same upstream usage.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from serving.servers.routers import anthropic_messages as amod
from serving.storage.utils import calculate_cost
from serving.utils.token_utils import normalize_usage
from tests.servers.conftest import ANTHROPIC_TEST_API_KEY

pytestmark = pytest.mark.asyncio

OPENAI_MODEL = "glm-4.7"
NATIVE_MODEL = "claude-opus-4.7"
AUTH = {"x-api-key": ANTHROPIC_TEST_API_KEY}
ANTHROPIC_USAGE_KEYS = {
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
}
THINKING = "Let me think about this carefully. " * 20

# name -> (upstream usage, expected log reasoning, expected Anthropic output_tokens)
USAGE_CASES: dict[str, tuple[dict[str, Any], int | None, int]] = {
    "unreported": (
        {"prompt_tokens": 1000, "completion_tokens": 300, "total_tokens": 1300},
        None,
        300,
    ),
    "reported-zero": (
        {
            "prompt_tokens": 1000,
            "completion_tokens": 300,
            "total_tokens": 1300,
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
        0,
        300,
    ),
    "included-in-completion": (
        {
            "prompt_tokens": 1000,
            "completion_tokens": 300,
            "total_tokens": 1300,
            "completion_tokens_details": {"reasoning_tokens": 250},
        },
        250,
        300,
    ),
    "outside-completion": (
        {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "total_tokens": 1500,
            "completion_tokens_details": {"reasoning_tokens": 400},
        },
        400,
        500,
    ),
    # No total: whether completion includes reasoning cannot be proved, and
    # both surfaces apply the same documented rule (bill completion + reasoning).
    "total-missing": (
        {
            "prompt_tokens": 1000,
            "completion_tokens": 300,
            "completion_tokens_details": {"reasoning_tokens": 250},
        },
        250,
        550,
    ),
}


def _adapter(client):
    services = client._transport.app.state.services
    adapter, _weight = services.router.routes[OPENAI_MODEL].adapters[0]
    return adapter


def _pricing(client) -> dict[str, str]:
    return dict(_adapter(client).config.pricing)


def _capture_logs(client) -> tuple[dict[str, dict[str, Any]], asyncio.Event]:
    """Record every api_logs write by request id; also record quota increments."""
    services = client._transport.app.state.services
    rows: dict[str, dict[str, Any]] = {}
    written = asyncio.Event()

    async def fake_log_request(**kwargs):
        rows[kwargs["request_id"]] = kwargs
        written.set()

    services.log_store.log_request = fake_log_request
    services.operational_store.increment_user_cost = AsyncMock()
    return rows, written


async def _wait_for(rows: dict, count: int) -> None:
    for _ in range(200):
        if len(rows) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {count} log rows, got {len(rows)}")


async def _wait_for_increments(mock: AsyncMock, count: int) -> None:
    for _ in range(200):
        if mock.await_count >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {count} cost increments, got {mock.await_count}")


def _completion(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-r",
        "object": "chat.completion",
        "model": OPENAI_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "reasoning_content": THINKING,
                    "content": "The answer.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


def _sse_chunks(usage: dict[str, Any] | None) -> list[bytes]:
    def chunk(delta: dict[str, Any], finish: str | None = None, **extra: Any) -> bytes:
        payload = {
            "id": "chatcmpl-r",
            "object": "chat.completion.chunk",
            "model": OPENAI_MODEL,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            **extra,
        }
        return f"data: {json.dumps(payload)}\n\n".encode()

    chunks = [
        chunk({"role": "assistant", "reasoning_content": THINKING}),
        chunk({"content": "The answer."}),
        chunk({}, "stop"),
    ]
    if usage is not None:
        chunks.append(
            f"data: {json.dumps({'id': 'chatcmpl-r', 'choices': [], 'usage': usage})}\n\n".encode()
        )
    chunks.append(b"data: [DONE]\n\n")
    return chunks


def _fake_session(pick):
    """``AsyncHTTPClient._ensure_session`` whose POST streams ``pick(json)``."""

    class _Content:
        def __init__(self, chunks):
            self._chunks = chunks

        async def iter_chunked(self, n):
            for c in self._chunks:
                await asyncio.sleep(0)
                yield c

        async def iter_any(self):
            for c in self._chunks:
                yield c

    class _Resp:
        status = 200
        headers: dict = {"Content-Type": "text/event-stream"}  # noqa: RUF012

        def __init__(self, chunks):
            self.content = _Content(chunks)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    class _Session:
        def post(self, url, json=None, headers=None, timeout=None):
            return _Resp(pick(json))

    async def ensure_session(self):
        return _Session()

    return ensure_session


def _body(text: str = "hi", *, stream: bool = False) -> dict[str, Any]:
    return {
        "model": OPENAI_MODEL,
        "max_tokens": 1024,
        "stream": stream,
        "messages": [{"role": "user", "content": text}],
    }


def _assert_logged(row: dict[str, Any], upstream: dict[str, Any], reasoning: int | None):
    usage = row["usage"]
    assert usage["prompt_tokens"] == upstream["prompt_tokens"]
    # Logged as the upstream reported it, like the chat surface: the reported
    # total is the evidence billing reads, so none is made up here.
    assert usage["completion_tokens"] == upstream["completion_tokens"]
    assert usage["total_tokens"] == upstream.get("total_tokens", 0)
    if reasoning is None:
        assert "reasoning_tokens" not in usage
    else:
        assert usage["reasoning_tokens"] == reasoning
    assert set(usage) <= {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    }


def _expected_cost(output_tokens: int, prompt_tokens: int, pricing: dict[str, str]) -> float:
    return (
        prompt_tokens * float(pricing["prompt"]) + output_tokens * float(pricing["completion"])
    ) / 1_000_000


@pytest.mark.parametrize("case", list(USAGE_CASES))
async def test_non_streaming_logs_and_bills_reasoning_like_chat(
    anthropic_test_client, monkeypatch, case
):
    upstream, reasoning, output_tokens = USAGE_CASES[case]

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        return _completion(upstream)

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)
    rows, _ = _capture_logs(anthropic_test_client)
    services = anthropic_test_client._transport.app.state.services

    r = await anthropic_test_client.post("/v1/messages", json=_body(), headers=AUTH)

    assert r.status_code == 200
    out = r.json()
    # The client sees plain Anthropic usage: no reasoning field, no total, and
    # no internal record; thinking counted inside output_tokens.
    assert set(out["usage"]) <= ANTHROPIC_USAGE_KEYS
    assert out["usage"] == {"input_tokens": 1000, "output_tokens": output_tokens}
    assert [b["type"] for b in out["content"]] == ["thinking", "text"]

    await _wait_for(rows, 1)
    (row,) = rows.values()
    _assert_logged(row, upstream, reasoning)
    assert row["response"] == out

    # Same charge as the chat surface for this upstream usage: the chat route
    # logs and bills normalize_usage() of the adapter's own response.
    chat = await _adapter(anthropic_test_client).chat_completion(
        [{"role": "user", "content": "hi"}]
    )
    pricing = _pricing(anthropic_test_client)
    chat_cost = calculate_cost(normalize_usage(chat["usage"]), pricing)
    logged_cost = calculate_cost(row["usage"], row["pricing"])
    assert logged_cost == pytest.approx(chat_cost)
    assert logged_cost == pytest.approx(_expected_cost(output_tokens, 1000, pricing))
    increment = services.operational_store.increment_user_cost
    await _wait_for_increments(increment, 1)
    assert increment.await_args.args[1] == pytest.approx(chat_cost)


@pytest.mark.parametrize("case", list(USAGE_CASES))
async def test_streaming_logs_and_bills_reasoning_like_chat(
    anthropic_test_client, monkeypatch, case
):
    upstream, reasoning, output_tokens = USAGE_CASES[case]
    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(
        AsyncHTTPClient, "_ensure_session", _fake_session(lambda _json: _sse_chunks(upstream))
    )
    rows, _ = _capture_logs(anthropic_test_client)
    services = anthropic_test_client._transport.app.state.services

    wire = b""
    async with anthropic_test_client.stream(
        "POST", "/v1/messages", json=_body(stream=True), headers=AUTH
    ) as r:
        assert r.status_code == 200
        async for raw in r.aiter_bytes():
            wire += raw

    assert b"event: message_stop" in wire
    assert b"thinking_delta" in wire
    delta = next(
        json.loads(line[len(b"data: ") :])
        for line in wire.split(b"\n")
        if line.startswith(b"data: ") and b'"message_delta"' in line
    )
    assert set(delta["usage"]) <= ANTHROPIC_USAGE_KEYS
    assert delta["usage"] == {"input_tokens": 1000, "output_tokens": output_tokens}
    assert b"reasoning_tokens" not in wire
    assert amod.OPENAI_USAGE_SINK_KEY.encode() not in wire

    await _wait_for(rows, 1)
    (row,) = rows.values()
    _assert_logged(row, upstream, reasoning)
    assert not (row["metadata"] or {}).get("usage_estimated")

    # The chat stream's terminal usage chunk for the same upstream, as the
    # chat route bills it.
    chat_usage = None
    async for chunk in _adapter(anthropic_test_client).stream_chat_completion(
        [{"role": "user", "content": "hi"}], stream=True
    ):
        for line in chunk.splitlines():
            if line.startswith("data: {"):
                payload = json.loads(line[len("data: ") :])
                chat_usage = payload.get("usage") or chat_usage
    pricing = _pricing(anthropic_test_client)
    chat_cost = calculate_cost(normalize_usage(chat_usage), pricing)
    logged_cost = calculate_cost(row["usage"], row["pricing"])
    assert logged_cost == pytest.approx(chat_cost)
    assert logged_cost == pytest.approx(_expected_cost(output_tokens, 1000, pricing))
    increment = services.operational_store.increment_user_cost
    await _wait_for_increments(increment, 1)
    assert increment.await_args.args[1] == pytest.approx(chat_cost)


async def test_concurrent_streams_each_log_their_own_reasoning(anthropic_test_client, monkeypatch):
    """The per-request sink keeps one stream's counts out of another's row."""
    by_text = {
        "first": USAGE_CASES["included-in-completion"][0],
        "second": USAGE_CASES["outside-completion"][0],
        "third": USAGE_CASES["unreported"][0],
    }

    def pick(json_body):
        return _sse_chunks(by_text[json_body["messages"][-1]["content"]])

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _fake_session(pick))
    rows, _ = _capture_logs(anthropic_test_client)

    async def run(text: str) -> None:
        async with anthropic_test_client.stream(
            "POST", "/v1/messages", json=_body(text, stream=True), headers=AUTH
        ) as r:
            assert r.status_code == 200
            async for _ in r.aiter_bytes():
                pass

    await asyncio.gather(*(run(text) for text in by_text))
    await _wait_for(rows, 3)

    logged = {row["prompt"][-1]["content"]: row["usage"] for row in rows.values()}
    assert logged["first"]["reasoning_tokens"] == 250
    assert logged["second"]["reasoning_tokens"] == 400
    assert logged["second"]["completion_tokens"] == 100
    assert "reasoning_tokens" not in logged["third"]


async def test_concurrent_non_streaming_requests_each_log_their_own_reasoning(
    anthropic_test_client, monkeypatch
):
    by_text = {
        "first": USAGE_CASES["included-in-completion"][0],
        "second": USAGE_CASES["reported-zero"][0],
    }

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        await asyncio.sleep(0)
        return _completion(by_text[json["messages"][-1]["content"]])

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)
    rows, _ = _capture_logs(anthropic_test_client)

    responses = await asyncio.gather(
        *(
            anthropic_test_client.post("/v1/messages", json=_body(text), headers=AUTH)
            for text in by_text
        )
    )
    assert [r.status_code for r in responses] == [200, 200]
    await _wait_for(rows, 2)

    logged = {row["prompt"][-1]["content"]: row["usage"] for row in rows.values()}
    assert logged["first"]["reasoning_tokens"] == 250
    assert logged["second"]["reasoning_tokens"] == 0


async def test_stream_that_ends_without_usage_logs_reasoning_as_unknown(
    anthropic_test_client, monkeypatch
):
    """No reported usage: counts are the adapter's estimate, reasoning stays NULL.

    The thinking text is not turned into a reasoning count.
    """
    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(
        AsyncHTTPClient, "_ensure_session", _fake_session(lambda _json: _sse_chunks(None))
    )
    rows, _ = _capture_logs(anthropic_test_client)

    async with anthropic_test_client.stream(
        "POST", "/v1/messages", json=_body(stream=True), headers=AUTH
    ) as r:
        async for _ in r.aiter_bytes():
            pass

    await _wait_for(rows, 1)
    (row,) = rows.values()
    assert "reasoning_tokens" not in row["usage"]


async def test_native_anthropic_logs_reasoning_as_not_separately_reported(
    anthropic_test_client, monkeypatch
):
    """Anthropic counts thinking inside output_tokens and reports no reasoning count."""
    upstream_resp = {
        "id": "msg_native_thinking",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [
            {"type": "thinking", "thinking": THINKING, "signature": "sig"},
            {"type": "text", "text": "Answer"},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 40, "output_tokens": 600},
    }

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        return upstream_resp

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)
    rows, _ = _capture_logs(anthropic_test_client)

    r = await anthropic_test_client.post(
        "/v1/messages",
        json={**_body(), "model": NATIVE_MODEL},
        headers=AUTH,
    )

    assert r.status_code == 200
    assert r.json() == upstream_resp
    await _wait_for(rows, 1)
    (row,) = rows.values()
    usage = row["usage"]
    assert "reasoning_tokens" not in usage
    assert (usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"]) == (
        40,
        600,
        640,
    )


async def test_native_anthropic_stream_logs_reasoning_as_not_separately_reported(
    anthropic_test_client, monkeypatch
):
    sse = [
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_t","model":"claude-opus-4-7",'
        b'"role":"assistant","content":[],"usage":{"input_tokens":40,"output_tokens":1}}}\n\n',
        b"event: content_block_start\n"
        b'data: {"type":"content_block_start","index":0,'
        b'"content_block":{"type":"thinking","thinking":""}}\n\n',
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"thinking_delta","thinking":"hmm"}}\n\n',
        b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
        b"event: message_delta\n"
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        b'"usage":{"output_tokens":600}}\n\n',
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]
    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _fake_session(lambda _json: sse))
    rows, _ = _capture_logs(anthropic_test_client)

    async with anthropic_test_client.stream(
        "POST",
        "/v1/messages",
        json={**_body(stream=True), "model": NATIVE_MODEL},
        headers=AUTH,
    ) as r:
        assert r.status_code == 200
        async for _ in r.aiter_bytes():
            pass

    await _wait_for(rows, 1)
    (row,) = rows.values()
    assert "reasoning_tokens" not in row["usage"]
    assert row["usage"]["completion_tokens"] == 600


def test_recovered_stream_usage_never_invents_a_reasoning_count():
    """A disconnect before the adapter flushed usage: estimated, reasoning unknown."""
    acc = {
        "content": [{"type": "thinking", "thinking": THINKING}, {"type": "text", "text": "x"}],
        "usage": {"input_tokens": 12, "output_tokens": 1},
    }
    usage, estimated = amod._resolve_stream_usage(
        {"input_tokens": 0, "output_tokens": 0}, acc, {"messages": []}
    )

    assert estimated is True
    assert amod.OPENAI_USAGE_SINK_KEY not in usage


def test_flushed_upstream_record_is_kept_and_not_estimated():
    record = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": 0,
    }
    usage, estimated = amod._resolve_stream_usage(
        {"input_tokens": 0, "output_tokens": 0, amod.OPENAI_USAGE_SINK_KEY: record},
        {"content": [{"type": "text", "text": "partial reply"}]},
        {"messages": [{"role": "user", "content": "hello"}]},
    )

    # A reported all-zero usage is a measurement, not a lost one to estimate.
    assert estimated is False
    assert usage[amod.OPENAI_USAGE_SINK_KEY] == record
    assert usage[amod.OPENAI_USAGE_SINK_KEY] is not record
