"""What the serving layer tells a dispatch's first-token watch (``dispatch_watch``).

The reports themselves, and the adapters that make the first-output one. Each
adapter must speak up at the first frame of generated output it reads from the
upstream, before whatever it then does with that output -- a stream processor
holding an XML tool call, the Claude adapters keeping a tool call's JSON -- can
hide it from the router. The limiter's reports are pinned in
``test_upstream_limiter.py``.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest

from serving.adapters.anthropic import AnthropicAdapter
from serving.adapters.base import ModelConfig
from serving.adapters.claude import ClaudeAdapter
from serving.adapters.dispatch_watch import report_first_token, report_queued, report_sent
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.http import AsyncHTTPClient
from serving.utils import context as req_ctx

MESSAGES = [{"role": "user", "content": "weather in Paris?"}]


class _Watch:
    """Records each report, with how many upstream frames had been sent by then."""

    def __init__(self, sent: list[Any] | None = None, *, fail: bool = False) -> None:
        self.events: list[tuple[str, int]] = []
        self._sent = sent if sent is not None else []
        self._fail = fail

    def _record(self, event: str) -> None:
        self.events.append((event, len(self._sent)))
        if self._fail:
            raise RuntimeError("broken watch")

    def on_queued(self) -> None:
        self._record("queued")

    def on_sent(self) -> None:
        self._record("sent")

    def on_first_token(self) -> None:
        self._record("first_token")

    def first_tokens(self) -> list[int]:
        """Upstream frames sent when each first-output report arrived."""
        return [frames for event, frames in self.events if event == "first_token"]


@contextmanager
def _watched(watch: Any):
    with req_ctx.push(**{req_ctx.UPSTREAM_DISPATCH_WATCH: watch}):
        yield


# ------------------------------------------------------------------ reports


def test_without_a_watch_every_report_is_a_no_op():
    report_queued()
    report_sent()
    report_first_token()
    with _watched(None):
        report_first_token()


def test_each_report_reaches_its_hook():
    watch = _Watch()

    with _watched(watch):
        report_queued()
        report_sent()
        report_first_token()

    assert [event for event, _ in watch.events] == ["queued", "sent", "first_token"]


def test_a_broken_watch_is_logged_not_raised(caplog):
    watch = _Watch(fail=True)

    with _watched(watch), caplog.at_level("ERROR", logger="serving.adapters.dispatch_watch"):
        report_first_token()

    failures = [r for r in caplog.records if r.getMessage() == "upstream_dispatch_watch_failed"]
    assert [record.hook for record in failures] == ["on_first_token"]


# ------------------------------------------------------ OpenAI-compatible


def _openai_compat(processor: str) -> OpenAICompatAdapter:
    adapter = OpenAICompatAdapter(
        ModelConfig(
            id="glm-4.7",
            name="GLM 4.7",
            provider="local-glm",
            base_url="http://localhost:8000/v1",
            api_key="k",
            processor=processor,
        )
    )
    # Its own client: see ``test_upstream_limiter._adapter`` for why not the shared one.
    adapter.http = AsyncHTTPClient()
    return adapter


def _frame(delta: dict[str, Any], finish_reason: str | None = None) -> str:
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "glm-4.7",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _serve_openai(monkeypatch, adapter: OpenAICompatAdapter, frames: list[str]) -> list[str]:
    sent: list[str] = []

    async def upstream(*_args: Any, **_kwargs: Any):
        for frame in frames:
            sent.append(frame)
            yield frame

    monkeypatch.setattr(adapter.http, "stream_post", upstream)
    return sent


async def test_openai_compat_reports_output_its_processor_holds_back(monkeypatch):
    adapter = _openai_compat("glm")
    frames = [
        _frame({"role": "assistant", "content": ""}),
        _frame({"content": "<tool_call>get_weather\n"}),
        _frame({"content": "<arg_key>city</arg_key>\n<arg_value>Paris</arg_value>\n"}),
        _frame({"content": "</tool_call>"}),
        _frame({}, finish_reason="stop"),
        "data: [DONE]\n\n",
    ]
    sent = _serve_openai(monkeypatch, adapter, frames)
    watch = _Watch(sent)

    with _watched(watch):
        stream = adapter.stream_chat_completion(MESSAGES)
        await stream.__anext__()
        frames_at_first_chunk = len(sent)
        [chunk async for chunk in stream]

    # The processor let nothing through until the tool call was complete...
    assert frames_at_first_chunk > 2
    # ...but the watch heard at the frame that opened it, and only then.
    assert watch.first_tokens() == [2]


@pytest.mark.parametrize(
    "delta",
    [
        {"content": "hi"},
        {"reasoning_content": "Let me think."},
        {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "f", "arguments": ""}}]},
    ],
    ids=["content", "reasoning", "tool-call"],
)
async def test_openai_compat_reports_any_kind_of_output(monkeypatch, delta):
    adapter = _openai_compat("default")
    frames = [
        _frame({"role": "assistant", "content": ""}),
        _frame(delta),
        _frame({"content": "more"}),
        _frame({}, finish_reason="stop"),
        "data: [DONE]\n\n",
    ]
    watch = _Watch(_serve_openai(monkeypatch, adapter, frames))

    with _watched(watch):
        [chunk async for chunk in adapter.stream_chat_completion(MESSAGES)]

    assert watch.first_tokens() == [2]


async def test_openai_compat_reports_nothing_for_a_stream_without_output(monkeypatch):
    adapter = _openai_compat("default")
    frames = [
        _frame({"role": "assistant", "content": ""}),
        _frame({}, finish_reason="stop"),
        "data: [DONE]\n\n",
    ]
    watch = _Watch(_serve_openai(monkeypatch, adapter, frames))

    with _watched(watch):
        [chunk async for chunk in adapter.stream_chat_completion(MESSAGES)]

    assert watch.first_tokens() == []


# ------------------------------------------------------------ Claude family

# A response that opens with a tool call: both Claude adapters keep its JSON
# until the message ends, so nothing reaches the router before then.
_TOOL_CALL_EVENTS: list[dict[str, Any]] = [
    {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "model": "claude",
            "role": "assistant",
            "content": [],
            "usage": {"input_tokens": 5, "output_tokens": 0},
        },
    },
    {"type": "ping"},
    {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}},
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": '{"city": '},
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": '"Paris"}'},
    },
    {"type": "content_block_stop", "index": 0},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use"},
        "usage": {"output_tokens": 12},
    },
    {"type": "message_stop"},
]
#: ``content_block_start`` is the third event: the first one carrying output.
_FIRST_OUTPUT_EVENT = 3


def _tool_calls(chunks: list[str]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for chunk in chunks:
        text = chunk if isinstance(chunk, str) else chunk.decode()
        if not text.startswith("data: ") or text.strip() == "data: [DONE]":
            continue
        for choice in json.loads(text[len("data: ") :]).get("choices") or []:
            calls.extend((choice.get("delta") or {}).get("tool_calls") or [])
    return calls


async def test_anthropic_reports_a_tool_call_it_holds_back(monkeypatch):
    sent: list[bytes] = []

    class _Content:
        async def iter_any(self):
            for event in _TOOL_CALL_EVENTS:
                frame = f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                sent.append(frame)
                yield frame

    class _Response:
        status = 200
        content = _Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc: Any):
            return None

    class _Session:
        def post(self, *_args: Any, **_kwargs: Any):
            return _Response()

    async def fake_ensure_session(_self):
        return _Session()

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", fake_ensure_session)
    adapter = AnthropicAdapter(
        ModelConfig(
            id="claude-opus-4.7",
            name="Claude Opus 4.7",
            provider="anthropic",
            base_url="https://api.anthropic.com",
            provider_model_id="claude-opus-4-7",
            api_keys=["sk-ant-test"],
            supports_tools=True,
        )
    )
    watch = _Watch(sent)

    with _watched(watch):
        stream = adapter.stream_chat_completion(MESSAGES)
        chunks = [await stream.__anext__()]
        frames_at_first_chunk = len(sent)
        chunks += [chunk async for chunk in stream]

    assert frames_at_first_chunk == len(_TOOL_CALL_EVENTS)
    assert [call["function"]["name"] for call in _tool_calls(chunks)] == ["get_weather"]
    assert watch.first_tokens() == [_FIRST_OUTPUT_EVENT]


async def test_claude_reports_a_tool_call_it_holds_back():
    adapter = ClaudeAdapter(
        ModelConfig(
            id="claude-sonnet-4-6",
            name="Claude Sonnet 4.6",
            provider="claude",
            base_url="https://vertex.example.com",
            api_key="test-api-key",
            supports_tools=True,
        )
    )
    adapter.http = MagicMock()
    sent: list[str] = []

    async def upstream(*_args: Any, **_kwargs: Any):
        for event in _TOOL_CALL_EVENTS:
            line = json.dumps(event)
            sent.append(line)
            yield line

    adapter.http.stream_post = upstream
    watch = _Watch(sent)

    with _watched(watch):
        stream = adapter.stream_chat_completion(MESSAGES)
        chunks = [await stream.__anext__()]
        frames_at_first_chunk = len(sent)
        chunks += [chunk async for chunk in stream]

    assert frames_at_first_chunk == len(_TOOL_CALL_EVENTS)
    assert [call["function"]["name"] for call in _tool_calls(chunks)] == ["get_weather"]
    assert watch.first_tokens() == [_FIRST_OUTPUT_EVENT]
