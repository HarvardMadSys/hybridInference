"""End-to-end tests for the Anthropic Messages router (non-streaming)."""

from __future__ import annotations

import pytest

NATIVE_MODEL = "claude-opus-4.7"
OPENAI_MODEL = "glm-4.7"
ANTHROPIC_UPSTREAM = "https://api.anthropic.com/v1/messages"
ZHIPU_UPSTREAM = "https://example-zhipu.test/chat/completions"


def _auth():
    from test.servers.conftest import ANTHROPIC_TEST_API_KEY

    return {"x-api-key": ANTHROPIC_TEST_API_KEY}


@pytest.mark.asyncio
async def test_v1_messages_native_identity_passthrough(anthropic_test_client, monkeypatch):
    upstream_resp = {
        "id": "msg_native",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "Hi"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 1},
    }

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        return upstream_resp

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    body = {
        "model": NATIVE_MODEL,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 200
    assert r.json() == upstream_resp


@pytest.mark.asyncio
async def test_anthropic_v1_messages_alias_reaches_same_handler(anthropic_test_client, monkeypatch):
    upstream_resp = {
        "id": "msg_alias",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "via alias"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        return upstream_resp

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    body = {
        "model": NATIVE_MODEL,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = await anthropic_test_client.post("/anthropic/v1/messages", json=body, headers=_auth())
    assert r.status_code == 200
    assert r.json()["content"][0]["text"] == "via alias"


@pytest.mark.asyncio
async def test_openai_backend_translated(anthropic_test_client, monkeypatch):
    """Anthropic-format -> glm-4.7 (zhipu) -> translation."""
    openai_resp = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": OPENAI_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Translated reply"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
    }

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        return openai_resp

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    body = {
        "model": OPENAI_MODEL,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 200
    out = r.json()
    assert out["type"] == "message"
    assert out["content"][0]["text"] == "Translated reply"
    assert out["usage"]["input_tokens"] == 9


@pytest.mark.asyncio
async def test_alias_model_id_resolves(anthropic_test_client, monkeypatch):
    upstream_resp = {
        "id": "msg_a",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        return upstream_resp

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    body = {
        "model": "claude-3-opus-latest",
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_unknown_model_returns_anthropic_format_404(anthropic_test_client):
    body = {
        "model": "no-such-model",
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 404
    err = r.json()
    assert err["type"] == "error"
    assert err["error"]["type"] == "not_found_error"


@pytest.mark.asyncio
async def test_v1_messages_native_streaming_passthrough(anthropic_test_client, monkeypatch):
    upstream_sse = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_s","model":"claude-opus-4-7",'
        b'"role":"assistant","content":[],"usage":{"input_tokens":4,"output_tokens":0}}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}\n\n'
        b"event: message_delta\n"
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n'
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n'
    )

    class _FakeContent:
        async def iter_any(self):
            yield upstream_sse

    class _FakeResp:
        status = 200
        content = _FakeContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class _FakeSession:
        def post(self, url, json=None, headers=None, timeout=None):
            return _FakeResp()

    async def fake_ensure_session(self):
        return _FakeSession()

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", fake_ensure_session)

    body = {
        "model": NATIVE_MODEL,
        "max_tokens": 50,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    async with anthropic_test_client.stream(
        "POST", "/v1/messages", json=body, headers=_auth()
    ) as r:
        assert r.status_code == 200
        collected = b""
        async for chunk in r.aiter_bytes():
            collected += chunk
    assert collected == upstream_sse


@pytest.mark.asyncio
async def test_v1_messages_translated_streaming(anthropic_test_client, monkeypatch):
    """Anthropic-format stream request -> glm-4.7 -> translated SSE."""
    openai_sse = (
        b'data: {"id":"x","object":"chat.completion.chunk","model":"glm-4.7",'
        b'"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
        b'data: {"id":"x","object":"chat.completion.chunk","model":"glm-4.7",'
        b'"choices":[{"index":0,"delta":{"content":"Hi"}}]}\n\n'
        b'data: {"id":"x","object":"chat.completion.chunk","model":"glm-4.7",'
        b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}\n\n'
    )

    class _FakeContent:
        async def iter_any(self):
            yield openai_sse

        async def iter_chunked(self, n):
            yield openai_sse

    class _FakeResp:
        status = 200
        content = _FakeContent()
        headers: dict = {"Content-Type": "text/event-stream"}  # noqa: RUF012

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class _FakeSession:
        def post(self, url, json=None, headers=None, timeout=None):
            return _FakeResp()

    async def fake_ensure_session(self):
        return _FakeSession()

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", fake_ensure_session)

    body = {
        "model": OPENAI_MODEL,
        "max_tokens": 50,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    async with anthropic_test_client.stream(
        "POST", "/v1/messages", json=body, headers=_auth()
    ) as r:
        assert r.status_code == 200
        collected = b""
        async for chunk in r.aiter_bytes():
            collected += chunk
    assert b"event: message_start" in collected
    assert b"event: message_stop" in collected
    # The translator emits text deltas; assert text "Hi" appears.
    assert b'"Hi"' in collected


@pytest.mark.asyncio
async def test_cache_control_dropped_for_openai_backend(anthropic_test_client, monkeypatch, caplog):
    openai_resp = {
        "id": "x",
        "object": "chat.completion",
        "model": OPENAI_MODEL,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        return openai_resp

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    body = {
        "model": OPENAI_MODEL,
        "max_tokens": 50,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}],
            }
        ],
        "thinking": {"type": "enabled", "budget_tokens": 1024},
    }
    caplog.set_level("WARNING", logger="serving.servers.routers.anthropic_messages")
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 200
    # Warning logged for both dropped fields.
    matches = [
        rec
        for rec in caplog.records
        if "cache_control" in rec.getMessage() and "thinking" in rec.getMessage()
    ]
    assert matches, (
        f"Expected warning mentioning cache_control + thinking; got: {[rec.getMessage() for rec in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_missing_auth_returns_anthropic_format(anthropic_test_client):
    r = await anthropic_test_client.post(
        "/v1/messages",
        json={
            "model": NATIVE_MODEL,
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 401
    err = r.json()
    assert err["type"] == "error"
    assert err["error"]["type"] == "authentication_error"


@pytest.mark.asyncio
async def test_invalid_auth_returns_anthropic_format(anthropic_test_client):
    r = await anthropic_test_client.post(
        "/v1/messages",
        headers={"x-api-key": "hyi-not-a-real-key"},
        json={
            "model": NATIVE_MODEL,
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "authentication_error"


@pytest.mark.asyncio
async def test_cache_control_preserved_for_native_backend(anthropic_test_client, monkeypatch):
    """Native (kind: anthropic) backend gets cache_control passed through unchanged."""
    upstream_resp = {
        "id": "msg_z",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    captured: dict = {}

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        captured["json"] = json
        return upstream_resp

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    body = {
        "model": NATIVE_MODEL,
        "max_tokens": 50,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}],
            }
        ],
    }
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 200
    # cache_control must reach upstream verbatim on the native path.
    assert captured["json"]["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
