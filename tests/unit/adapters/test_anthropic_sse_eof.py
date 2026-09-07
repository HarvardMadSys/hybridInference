"""Anthropic SSE bodies whose last event is not followed by a blank line.

Both of the adapter's frame loops accumulate bytes and only parse a frame on the
``\\n\\n`` delimiter, so an upstream that closes the body straight after its last
event left that event unparsed. That last event is ``message_delta`` /
``message_stop`` -- where the output-token count and the stop reason live -- so
the loss was silent and showed up as under-reported usage (and cost) rather than
an error.
"""

from __future__ import annotations

import json

import pytest

from serving.adapters.anthropic import AnthropicAdapter
from serving.adapters.base import ModelConfig
from serving.http import AsyncHTTPClient

_HEAD = (
    b"event: message_start\n"
    b'data: {"type":"message_start","message":{"id":"msg_z","model":"claude-opus-4-7",'
    b'"role":"assistant","content":[],"usage":{"input_tokens":11,"output_tokens":0}}}\n\n'
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
)
# The final event, deliberately missing SSE's terminating blank line.
_UNTERMINATED_TAIL = (
    b"event: message_delta\n"
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    b'"usage":{"output_tokens":9}}\n'
)


def _cfg() -> ModelConfig:
    return ModelConfig(
        id="claude-opus-4.7",
        name="Claude Opus 4.7",
        provider="anthropic",
        base_url="https://api.anthropic.com",
        provider_model_id="claude-opus-4-7",
        api_keys=["sk-ant-test"],
    )


def _serve(monkeypatch, body: bytes) -> None:
    class _FakeContent:
        async def iter_any(self):
            yield body

    class _FakeResp:
        status = 200
        content = _FakeContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class _FakeSession:
        def post(self, *_a, **_k):
            return _FakeResp()

    async def fake_ensure_session(self):
        return _FakeSession()

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", fake_ensure_session)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_translation_counts_an_unterminated_final_event(monkeypatch):
    """``message_delta``'s output tokens and stop reason survive the body end."""
    _serve(monkeypatch, _HEAD + _UNTERMINATED_TAIL)

    chunks = [
        c if isinstance(c, str) else c.decode()
        async for c in AnthropicAdapter(_cfg()).stream_chat_completion(
            messages=[{"role": "user", "content": "hi"}], max_tokens=64, stream=True
        )
    ]

    payloads = [
        json.loads(c[len("data: ") :])
        for c in "".join(chunks).split("\n\n")
        if c.startswith("data: ") and c[len("data: ") :].strip() not in ("", "[DONE]")
    ]
    final = payloads[-1]
    assert final["usage"]["completion_tokens"] == 9
    assert final["choices"][0]["finish_reason"] == "stop"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_identity_passthrough_counts_an_unterminated_final_event(monkeypatch):
    """Usage is sniffed from the trailing event, and the wire is untouched."""
    body = _HEAD + _UNTERMINATED_TAIL
    _serve(monkeypatch, body)

    adapter = AnthropicAdapter(_cfg())
    out = b""
    async for chunk in adapter.stream_messages(
        {
            "model": "claude-opus-4.7",
            "max_tokens": 100,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
        request_id="req_eof",
    ):
        out += chunk

    # The client gets exactly what the upstream sent -- completing the frame is
    # our accounting's business, not something we synthesize onto the wire.
    assert out == body
    assert adapter.last_stream_usage["output_tokens"] == 9
    assert adapter.last_stream_usage["input_tokens"] == 11


@pytest.mark.unit
@pytest.mark.asyncio
async def test_properly_terminated_body_is_unaffected(monkeypatch):
    """The happy path must not double-count the final event."""
    _serve(monkeypatch, _HEAD + _UNTERMINATED_TAIL + b"\n")

    adapter = AnthropicAdapter(_cfg())
    async for _ in adapter.stream_messages(
        {
            "model": "claude-opus-4.7",
            "max_tokens": 100,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
        request_id="req_ok",
    ):
        pass

    assert adapter.last_stream_usage["output_tokens"] == 9
