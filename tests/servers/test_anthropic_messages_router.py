"""End-to-end tests for the Anthropic Messages router (non-streaming)."""

from __future__ import annotations

import json

import pytest

NATIVE_MODEL = "claude-opus-4.7"
OPENAI_MODEL = "glm-4.7"
ANTHROPIC_UPSTREAM = "https://api.anthropic.com/v1/messages"
ZAI_UPSTREAM = "https://example-zai.test/chat/completions"


def _auth():
    from tests.servers.conftest import ANTHROPIC_TEST_API_KEY

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
    """Anthropic-format -> glm-4.7 (zai) -> translation."""
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
async def test_upstream_timeout_returns_504_not_502(anthropic_test_client, monkeypatch):
    """A slow upstream (timeout) surfaces as 504, not a generic 502 api_error."""
    import asyncio

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        raise asyncio.TimeoutError

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    body = {
        "model": OPENAI_MODEL,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 504
    err = r.json()
    assert err["type"] == "error"
    # 504 has no dedicated Anthropic error type; api_error is the retryable default.
    assert err["error"]["type"] == "api_error"


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
async def test_disabled_model_returns_anthropic_format_404(anthropic_test_client, monkeypatch):
    from serving.servers.auth import verify_api_key

    app = anthropic_test_client._transport.app
    app.dependency_overrides[verify_api_key] = lambda: {
        "user_id": "u1",
        "role": "free",
        "authenticated": True,
        "is_admin": False,
        "disabled_models": [OPENAI_MODEL],
    }

    body = {
        "model": OPENAI_MODEL,
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
async def test_openai_backend_repairs_non_object_tool_input(
    anthropic_test_client, monkeypatch, caplog
):
    captured = {}
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
        captured["payload"] = json
        return openai_resp

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    body = {
        "model": OPENAI_MODEL,
        "max_tokens": 50,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "ChromeRelayReadDom",
                        "input": '{}""',
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01",
                        "content": "done",
                    }
                ],
            },
        ],
    }
    caplog.set_level("WARNING", logger="serving.servers.routers.anthropic_messages")
    response = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())

    assert response.status_code == 200
    arguments = captured["payload"]["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(arguments) == {}
    assert any(
        "Normalizing 1 non-object Anthropic tool_use.input" in record.getMessage()
        for record in caplog.records
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


@pytest.mark.asyncio
async def test_anthropic_beta_header_forwarded_on_native_passthrough(
    anthropic_test_client, monkeypatch
):
    """anthropic-beta header from inbound request must be forwarded to upstream on native path."""
    upstream_resp = {
        "id": "msg_beta",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    captured: dict = {}

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        captured["headers"] = headers
        return upstream_resp

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    body = {
        "model": NATIVE_MODEL,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    }
    req_headers = {**_auth(), "anthropic-beta": "prompt-caching-2024-07-31"}
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=req_headers)
    assert r.status_code == 200
    # anthropic-beta must reach the upstream Anthropic API.
    assert captured["headers"].get("anthropic-beta") == "prompt-caching-2024-07-31"
    # Auth headers must be our controlled values, not forwarded from client.
    assert captured["headers"].get("x-api-key") == "sk-ant-test"


@pytest.mark.asyncio
async def test_sanitize_openai_backend_drops_top_k_container_extra_metadata(
    anthropic_test_client, monkeypatch, caplog
):
    """top_k, container, and extra metadata keys are dropped and warned for OpenAI backends."""
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
        "messages": [{"role": "user", "content": "hi"}],
        "top_k": 40,
        "container": "my-container",
        "metadata": {"user_id": "u1", "session_id": "s99"},
    }
    caplog.set_level("WARNING", logger="serving.servers.routers.anthropic_messages")
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 200
    # Warning must mention the dropped fields.
    warning_text = " ".join(rec.getMessage() for rec in caplog.records)
    assert "top_k" in warning_text
    assert "container" in warning_text
    assert "metadata" in warning_text


@pytest.mark.asyncio
async def test_streaming_records_ttft_ms_on_first_content_block_delta(
    anthropic_test_client, monkeypatch
):
    """Streaming /v1/messages must capture ttft_ms on the first content_block_delta."""
    upstream_sse = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_t","model":"claude-opus-4-7",'
        b'"role":"assistant","content":[],"usage":{"input_tokens":4,"output_tokens":0}}}\n\n'
        b"event: content_block_start\n"
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
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

    captured: dict = {}

    from serving.servers.routers import anthropic_messages as amod

    def fake_schedule(log_store, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(amod, "_schedule_log_store_task", fake_schedule)

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
        async for _ in r.aiter_bytes():
            pass

    assert "ttft_ms" in captured
    assert isinstance(captured["ttft_ms"], int)
    assert captured["ttft_ms"] >= 0
    assert captured["params"].get("stream") is True


@pytest.mark.asyncio
async def test_streaming_ttft_ms_none_when_no_content_delta(anthropic_test_client, monkeypatch):
    """If the stream never emits content_block_delta, ttft_ms must remain None."""
    upstream_sse = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_n","model":"claude-opus-4-7",'
        b'"role":"assistant","content":[],"usage":{"input_tokens":4,"output_tokens":0}}}\n\n'
        b"event: content_block_start\n"
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
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

    captured: dict = {}

    from serving.servers.routers import anthropic_messages as amod

    def fake_schedule(log_store, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(amod, "_schedule_log_store_task", fake_schedule)

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
        async for _ in r.aiter_bytes():
            pass

    assert captured.get("ttft_ms") is None


@pytest.mark.asyncio
async def test_streaming_ttft_ms_handles_split_event_name(anthropic_test_client, monkeypatch):
    """ttft_ms detection must survive `content_block_delta` split across two raw chunks."""
    chunk_a = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_x","model":"claude-opus-4-7",'
        b'"role":"assistant","content":[],"usage":{"input_tokens":4,"output_tokens":0}}}\n\n'
        b"event: content_block_d"
    )
    chunk_b = (
        b"elta\n"
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )

    class _FakeContent:
        async def iter_any(self):
            yield chunk_a
            yield chunk_b

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

    captured: dict = {}

    from serving.servers.routers import anthropic_messages as amod

    def fake_schedule(log_store, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(amod, "_schedule_log_store_task", fake_schedule)

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
        async for _ in r.aiter_bytes():
            pass

    assert isinstance(captured.get("ttft_ms"), int)


# ---------------------------------------------------------------------------
# DB-logging payload: prompt/response threading + cache-inclusive token math
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_streaming_logs_prompt_response_and_cache_inclusive_tokens(
    anthropic_test_client, monkeypatch
):
    """Non-streaming path must persist the request prompt + upstream response
    into the DB. ``prompt_tokens`` follows OpenAI semantics: it is the total
    input including the cached subset. Cache tokens are *also* stored in the
    dedicated cache_read_tokens / cache_write_tokens columns; calculate_cost
    subtracts them from prompt_tokens before applying prompt_price."""
    upstream_resp = {
        "id": "msg_log",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "Hello"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 7,
            "output_tokens": 3,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 50,
        },
    }

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        return upstream_resp

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    captured: dict = {}
    captured_event = __import__("asyncio").Event()

    async def fake_log_request(**kwargs):
        captured.update(kwargs)
        captured_event.set()

    services = anthropic_test_client._transport.app.state.services
    services.log_store.log_request = fake_log_request

    messages = [{"role": "user", "content": "hello there"}]
    tools = [
        {
            "name": "lookup",
            "description": "Lookup a thing",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }
    ]
    body = {
        "model": NATIVE_MODEL,
        "max_tokens": 50,
        "system": "Be concise.",
        "messages": messages,
        "tools": tools,
        "tool_choice": {"type": "auto"},
    }
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 200

    await __import__("asyncio").wait_for(captured_event.wait(), timeout=2.0)

    assert captured["prompt"] == messages
    assert captured["request_payload"] == body
    assert captured["params"]["tools"] == tools
    assert captured["params"]["tool_count"] == 1
    assert captured["response"] == upstream_resp
    usage = captured["usage"]
    # prompt_tokens = input_tokens + cache_read + cache_write (OpenAI semantic)
    assert usage["prompt_tokens"] == 7 + 100 + 50
    assert usage["completion_tokens"] == 3
    assert usage["total_tokens"] == 7 + 100 + 50 + 3
    assert usage["cache_read_tokens"] == 100
    assert usage["cache_write_tokens"] == 50


@pytest.mark.asyncio
async def test_streaming_logs_prompt_and_cache_separate_tokens(anthropic_test_client, monkeypatch):
    """Streaming path must persist the request prompt and accumulated response.
    ``prompt_tokens`` follows OpenAI semantics — it includes the cached subset
    (input + cache_read + cache_write). The cached subset is *also* stored
    separately in cache_read_tokens / cache_write_tokens; calculate_cost
    subtracts those before applying prompt_price so cache is not
    double-billed."""
    upstream_sse = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_s","model":"claude-opus-4-7",'
        b'"role":"assistant","content":[],"usage":{"input_tokens":4,"output_tokens":0,'
        b'"cache_read_input_tokens":20,"cache_creation_input_tokens":10}}}\n\n'
        b"event: content_block_start\n"
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
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

    captured: dict = {}
    captured_event = __import__("asyncio").Event()

    async def fake_log_request(**kwargs):
        captured.update(kwargs)
        captured_event.set()

    services = anthropic_test_client._transport.app.state.services
    services.log_store.log_request = fake_log_request

    messages = [{"role": "user", "content": "say hi"}]
    body = {"model": NATIVE_MODEL, "max_tokens": 50, "stream": True, "messages": messages}
    async with anthropic_test_client.stream(
        "POST", "/v1/messages", json=body, headers=_auth()
    ) as r:
        assert r.status_code == 200
        async for _ in r.aiter_bytes():
            pass

    await __import__("asyncio").wait_for(captured_event.wait(), timeout=2.0)

    assert captured["prompt"] == messages
    resp = captured["response"]
    assert resp is not None
    assert resp["id"] == "msg_s"
    assert resp["role"] == "assistant"
    assert resp["stop_reason"] == "end_turn"
    assert resp["content"] == [{"type": "text", "text": "hi"}]
    usage = captured["usage"]
    # prompt_tokens = input_tokens + cache_read + cache_write (OpenAI semantic)
    assert usage["prompt_tokens"] == 4 + 20 + 10
    assert usage["completion_tokens"] == 2
    assert usage["total_tokens"] == 4 + 20 + 10 + 2
    assert usage["cache_read_tokens"] == 20
    assert usage["cache_write_tokens"] == 10


@pytest.mark.asyncio
async def test_streaming_empty_completion_flagged_in_logged_metadata(
    anthropic_test_client, monkeypatch
):
    """A well-formed 200 stream with no content blocks is flagged, not logged
    identically to a normal completion.

    Seen in prod on zai/minimax at meaningful volume: the stream completes
    cleanly (message_start -> message_delta -> message_stop) but never emits
    a single content_block, so the client gets nothing back even though the
    request "succeeded".
    """
    upstream_sse = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_empty","model":"claude-opus-4-7",'
        b'"role":"assistant","content":[],"usage":{"input_tokens":50,"output_tokens":0}}}\n\n'
        b"event: message_delta\n"
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":0}}\n\n'
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

    captured: dict = {}
    captured_event = __import__("asyncio").Event()

    async def fake_log_request(**kwargs):
        captured.update(kwargs)
        captured_event.set()

    services = anthropic_test_client._transport.app.state.services
    services.log_store.log_request = fake_log_request

    messages = [{"role": "user", "content": "say hi"}]
    body = {"model": NATIVE_MODEL, "max_tokens": 50, "stream": True, "messages": messages}
    async with anthropic_test_client.stream(
        "POST", "/v1/messages", json=body, headers=_auth()
    ) as r:
        assert r.status_code == 200
        async for _ in r.aiter_bytes():
            pass

    await __import__("asyncio").wait_for(captured_event.wait(), timeout=2.0)

    assert captured["status_code"] == 200
    assert captured["error"] is None
    assert captured["metadata"]["empty_completion"] is True


@pytest.mark.asyncio
async def test_streaming_logged_response_reassembles_split_sse_frames(
    anthropic_test_client, monkeypatch
):
    """SSE events split across raw chunk boundaries must still aggregate into the logged response."""
    full_sse = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_split","model":"claude-opus-4-7",'
        b'"role":"assistant","content":[],"usage":{"input_tokens":3,"output_tokens":0}}}\n\n'
        b"event: content_block_start\n"
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" world"}}\n\n'
        b"event: content_block_stop\n"
        b'data: {"type":"content_block_stop","index":0}\n\n'
        b"event: message_delta\n"
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n'
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n'
    )
    # Split mid-JSON inside the second content_block_delta event so the chunk
    # boundary lands inside an SSE frame's `data:` line.
    split_at = full_sse.index(b'"text":" world"') + 5
    chunks = [full_sse[:split_at], full_sse[split_at:]]

    class _FakeContent:
        async def iter_any(self):
            for c in chunks:
                yield c

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

    captured: dict = {}
    captured_event = __import__("asyncio").Event()

    async def fake_log_request(**kwargs):
        captured.update(kwargs)
        captured_event.set()

    services = anthropic_test_client._transport.app.state.services
    services.log_store.log_request = fake_log_request

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
        async for _ in r.aiter_bytes():
            pass

    await __import__("asyncio").wait_for(captured_event.wait(), timeout=2.0)

    resp = captured["response"]
    assert resp is not None
    assert resp["id"] == "msg_split"
    assert resp["stop_reason"] == "end_turn"
    assert resp["content"] == [{"type": "text", "text": "hello world"}]


# ---------------------------------------------------------------------------
# Error logging: upstream status preservation and error= population
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_error_logs_non_empty_error_and_502(anthropic_test_client, monkeypatch):
    """Streaming generator that raises a generic Exception must log error= and status_code=502."""

    class _BrokenContent:
        async def iter_any(self):
            raise RuntimeError("upstream exploded")
            yield b""  # make this an async generator

    class _FakeResp:
        status = 200
        content = _BrokenContent()

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

    captured: dict = {}

    from serving.servers.routers import anthropic_messages as amod

    def fake_schedule(log_store, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(amod, "_schedule_log_store_task", fake_schedule)

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
        async for _ in r.aiter_bytes():
            pass

    assert captured.get("status_code") == 502
    assert captured.get("error") is not None
    assert len(captured["error"]) > 0
    # The operator-facing cause is captured separately and preserves the real
    # error text, while the user-facing `error` is the scrubbed generic message.
    assert captured.get("operator_error") and "upstream exploded" in captured["operator_error"]
    assert "upstream exploded" not in captured["error"]


@pytest.mark.asyncio
async def test_schedule_log_store_merges_operator_error_into_metadata():
    """operator_error lands in metadata.operator_error (operator-only), leaving the
    user-facing `error` untouched and the original metadata preserved."""
    import asyncio

    from serving.servers.routers import anthropic_messages as amod

    captured: dict = {}

    class _FakeStore:
        async def log_request(self, **kw):
            captured.update(kw)

    amod._schedule_log_store_task(
        _FakeStore(),
        request_id="amsg_x",
        model_id="glm-5.1",
        provider="zai",
        usage={},
        latency_ms=1,
        status_code=502,
        pricing={},
        metadata={"surface": "anthropic_messages"},
        params={},
        error="Internal server error (request_id: amsg_x)",
        operator_error="zai upstream 500: model overloaded",
    )
    for _ in range(20):
        await asyncio.sleep(0)
        if captured:
            break

    assert captured["error"] == "Internal server error (request_id: amsg_x)"
    assert captured["metadata"]["operator_error"] == "zai upstream 500: model overloaded"
    assert captured["metadata"]["surface"] == "anthropic_messages"  # original preserved


@pytest.mark.asyncio
async def test_non_streaming_client_response_error_429_logs_real_status(
    anthropic_test_client, monkeypatch
):
    """Non-streaming adapter.messages() raising ClientResponseError(429) must log status_code=429."""
    import aiohttp

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        raise aiohttp.ClientResponseError(
            request_info=None,
            history=None,
            status=429,
            message="rate limit exceeded",
        )

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    captured: dict = {}

    from serving.servers.routers import anthropic_messages as amod

    def fake_schedule(log_store, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(amod, "_schedule_log_store_task", fake_schedule)

    body = {
        "model": NATIVE_MODEL,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 429
    assert r.json()["error"]["type"] == "rate_limit_error"

    assert captured.get("status_code") == 429
    assert captured.get("error") is not None
    assert len(captured["error"]) > 0


@pytest.mark.asyncio
async def test_non_streaming_generic_exception_logs_502(anthropic_test_client, monkeypatch):
    """Non-streaming adapter.messages() raising a generic Exception must log status_code=502."""

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        raise ConnectionError("network gone")

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    captured: dict = {}

    from serving.servers.routers import anthropic_messages as amod

    def fake_schedule(log_store, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(amod, "_schedule_log_store_task", fake_schedule)

    body = {
        "model": NATIVE_MODEL,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "api_error"

    assert captured.get("status_code") == 502
    assert captured.get("error") is not None
    assert len(captured["error"]) > 0


@pytest.mark.asyncio
async def test_streaming_client_response_error_logs_upstream_status(
    anthropic_test_client, monkeypatch
):
    """Streaming generator raising ClientResponseError must log the real upstream status_code."""
    import aiohttp

    class _BrokenContent:
        async def iter_any(self):
            raise aiohttp.ClientResponseError(
                request_info=None,
                history=None,
                status=503,
                message="service overloaded",
            )
            yield b""  # make this an async generator

    class _FakeResp:
        status = 200
        content = _BrokenContent()

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

    captured: dict = {}

    from serving.servers.routers import anthropic_messages as amod

    def fake_schedule(log_store, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(amod, "_schedule_log_store_task", fake_schedule)

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
        async for _ in r.aiter_bytes():
            pass

    assert captured.get("status_code") == 503
    assert captured.get("error") is not None
    assert len(captured["error"]) > 0


# ---------------------------------------------------------------------------
# Streaming usage recovery (client disconnect before usage_sink flush)
# ---------------------------------------------------------------------------


def test_resolve_stream_usage_trusts_real_usage():
    """A completed stream (adapter flushed usage_sink) is used verbatim."""
    from serving.servers.routers import anthropic_messages as amod

    req = {
        "input_tokens": 12,
        "output_tokens": 5,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    usage, estimated = amod._resolve_stream_usage(req, {"usage": {"input_tokens": 999}}, None)
    assert usage["input_tokens"] == 12
    assert usage["output_tokens"] == 5
    assert estimated is False


def test_resolve_stream_usage_recovers_partial_from_response_acc():
    """Disconnect after message_start: real input_tokens recovered, output estimated.

    The message_start usage carries output_tokens=1 (the real Anthropic
    placeholder); the accumulated-content estimate must win over it.
    """
    from serving.servers.routers import anthropic_messages as amod

    empty = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    acc = {
        "usage": {"input_tokens": 37, "output_tokens": 1, "cache_read_input_tokens": 4},
        "content": [{"type": "text", "text": "partial answer here " * 10}],
    }
    payload = {"messages": [{"role": "user", "content": "hello"}]}
    usage, estimated = amod._resolve_stream_usage(empty, acc, payload)
    assert usage["input_tokens"] == 37  # real, from message_start
    assert usage["cache_read_input_tokens"] == 4
    assert usage["output_tokens"] > 1  # estimate beats the message_start placeholder
    assert estimated is True


def test_resolve_stream_usage_placeholder_output_does_not_suppress_estimate():
    """Regression: a long native partial response must not log output_tokens=1.

    Anthropic's message_start reports a placeholder output_tokens (e.g. 1); the
    terminal message_delta with the real count never arrives on a disconnect.
    """
    from serving.servers.routers import anthropic_messages as amod

    empty = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    acc = {
        "usage": {"input_tokens": 50, "output_tokens": 1},
        "content": [{"type": "text", "text": "word " * 500}],
    }
    usage, estimated = amod._resolve_stream_usage(empty, acc, None)
    assert usage["input_tokens"] == 50
    assert usage["output_tokens"] > 100  # ~500 words estimated, not the placeholder 1
    assert estimated is True


def test_resolve_stream_usage_keeps_real_observed_output_over_estimate():
    """When a real (large) output count was observed, don't downgrade to an estimate."""
    from serving.servers.routers import anthropic_messages as amod

    empty = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    acc = {
        "usage": {"input_tokens": 20, "output_tokens": 999},  # real, e.g. late message_delta
        "content": [{"type": "text", "text": "short"}],
    }
    usage, _ = amod._resolve_stream_usage(empty, acc, None)
    assert usage["output_tokens"] == 999


def test_bounded_text_tokens_uses_heuristic_above_cap():
    """Oversized text skips tiktoken and uses the cheap char heuristic."""
    from serving.servers.routers import anthropic_messages as amod

    big = "x" * (amod._ESTIMATE_CHAR_CAP + 8)
    assert amod._bounded_text_tokens(big) == max(1, len(big) // 4)
    assert amod._bounded_text_tokens("hello world") > 0


def test_resolve_stream_usage_estimates_input_on_pure_hang():
    """Disconnect before any byte: nothing in response_acc, estimate input from request."""
    from serving.servers.routers import anthropic_messages as amod

    empty = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    payload = {
        "system": "You are a helpful coding assistant.",
        "messages": [{"role": "user", "content": "Write a function to sort a list."}],
    }
    usage, estimated = amod._resolve_stream_usage(empty, None, payload)
    assert usage["input_tokens"] > 0  # estimated from system + messages
    assert usage["output_tokens"] == 0  # nothing was produced
    assert estimated is True


def test_resolve_stream_usage_zero_when_nothing_available():
    """No usage, no response, no payload -> stays zero, not flagged estimated."""
    from serving.servers.routers import anthropic_messages as amod

    empty = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    usage, estimated = amod._resolve_stream_usage(empty, None, None)
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert estimated is False


def test_estimate_request_input_tokens_folds_system_and_tools():
    """system prompt and tool schemas both contribute to the input estimate."""
    from serving.servers.routers import anthropic_messages as amod

    base = amod._estimate_request_input_tokens({"messages": [{"role": "user", "content": "hi"}]})
    with_system = amod._estimate_request_input_tokens(
        {
            "system": "a much longer system prompt " * 20,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    with_tools = amod._estimate_request_input_tokens(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "do_thing", "description": "x" * 200, "input_schema": {}}],
        }
    )
    assert with_system > base
    assert with_tools > base


@pytest.mark.asyncio
async def test_streaming_recovers_usage_on_client_disconnect(anthropic_test_client, monkeypatch):
    """A mid-stream abort (no end-of-stream usage flush) must not log 0/0.

    Simulates the production symptom: the upstream yields message_start (real
    input_tokens) then the connection is cut before the adapter flushes
    usage_sink. The router's finally must recover input_tokens from the
    forwarded stream and flag the row as estimated.
    """
    import asyncio

    # message_start carries the real Anthropic placeholder output_tokens=1; the
    # forwarded content must drive the output estimate, not the placeholder.
    upstream_sse = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_d","model":"claude-opus-4-7",'
        b'"role":"assistant","content":[],"usage":{"input_tokens":41,"output_tokens":1}}}\n\n'
        b"event: content_block_start\n"
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello there '
        b'this is a fairly long partial assistant response that was cut off mid-stream"}}\n\n'
    )

    class _FakeContent:
        async def iter_any(self):
            yield upstream_sse
            # Client/upstream disconnect before message_delta + message_stop:
            # the adapter never reaches its usage_sink.update().
            raise asyncio.CancelledError()

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

    captured: dict = {}

    from serving.servers.routers import anthropic_messages as amod

    def fake_schedule(log_store, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(amod, "_schedule_log_store_task", fake_schedule)

    body = {
        "model": NATIVE_MODEL,
        "max_tokens": 50,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    try:
        async with anthropic_test_client.stream(
            "POST", "/v1/messages", json=body, headers=_auth()
        ) as r:
            async for _ in r.aiter_bytes():
                pass
    except Exception:
        # Client may observe the truncated stream as a transport error; the
        # server-side finally (and its log) is what we assert on.
        pass

    assert captured, "_schedule_log_store_task should still run via the finally"
    usage = captured["usage"]
    assert usage["input_tokens"] == 41  # recovered from message_start, not 0
    assert usage["output_tokens"] > 1  # estimated from forwarded content, not the placeholder
    assert captured["metadata"].get("usage_estimated") is True


@pytest.mark.asyncio
async def test_referer_header_captured_in_logged_metadata(anthropic_test_client, monkeypatch):
    """The inbound Referer header is persisted into the request metadata so it is
    queryable as metadata->>'referer', mirroring user_agent."""
    upstream_resp = {
        "id": "msg_ref",
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

    captured: dict = {}

    from serving.servers.routers import anthropic_messages as amod

    def fake_schedule(log_store, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(amod, "_schedule_log_store_task", fake_schedule)

    body = {
        "model": NATIVE_MODEL,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    }
    headers = {**_auth(), "Referer": "https://example.com/playground"}
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=headers)
    assert r.status_code == 200

    assert captured, "_schedule_log_store_task should run on the success path"
    assert captured["metadata"]["referer"] == "https://example.com/playground"


# ---------------------------------------------------------------------------
# Streaming keepalive heartbeat + max-idle abort (slow-backend disconnect fix)
# ---------------------------------------------------------------------------


def _fake_session_from_iter(iter_factory):
    """Build a monkeypatch target: AsyncHTTPClient._ensure_session returning a
    session whose POST yields an SSE body driven by ``iter_factory``."""

    class _FakeContent:
        def iter_any(self):
            return iter_factory()

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

    return fake_ensure_session


@pytest.mark.asyncio
async def test_streaming_emits_keepalive_during_idle_gap(anthropic_test_client, monkeypatch):
    """A slow upstream (no first token for a while) gets keepalive heartbeats,
    and the real content still streams through once it arrives."""
    import asyncio

    from serving.servers.routers import anthropic_messages as amod

    monkeypatch.setattr(amod, "_KEEPALIVE_INTERVAL", 0.02)
    monkeypatch.setattr(amod, "_MAX_STREAM_IDLE", 100)

    full_sse = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_k","model":"claude-opus-4-7",'
        b'"role":"assistant","content":[],"usage":{"input_tokens":5,"output_tokens":1}}}\n\n'
        b"event: content_block_start\n"
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
        b"event: message_delta\n"
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n'
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n'
    )

    async def _iter():
        await asyncio.sleep(0.08)  # several keepalive intervals before first byte
        yield full_sse

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _fake_session_from_iter(_iter))

    captured: dict = {}
    monkeypatch.setattr(
        amod, "_schedule_log_store_task", lambda log_store, **kw: captured.update(kw)
    )

    body = {
        "model": NATIVE_MODEL,
        "max_tokens": 50,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    collected = b""
    async with anthropic_test_client.stream(
        "POST", "/v1/messages", json=body, headers=_auth()
    ) as r:
        assert r.status_code == 200
        async for raw in r.aiter_bytes():
            collected += raw

    assert b": keepalive" in collected  # heartbeat emitted during the idle gap
    assert b"message_start" in collected  # real content still delivered
    assert b"message_stop" in collected
    assert captured.get("status_code") == 200
    assert captured.get("error") is None


@pytest.mark.asyncio
async def test_streaming_aborts_after_max_idle(anthropic_test_client, monkeypatch):
    """If the upstream never sends data, keepalives stop at _MAX_STREAM_IDLE and
    the stream aborts with a 504 error event (usage stays zero -- not billed)."""
    import asyncio

    from serving.servers.routers import anthropic_messages as amod

    monkeypatch.setattr(amod, "_KEEPALIVE_INTERVAL", 0.02)
    monkeypatch.setattr(amod, "_MAX_STREAM_IDLE", 0.08)

    async def _iter():
        await asyncio.sleep(5)  # effectively never within the test window
        yield b""

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _fake_session_from_iter(_iter))

    captured: dict = {}
    monkeypatch.setattr(
        amod, "_schedule_log_store_task", lambda log_store, **kw: captured.update(kw)
    )

    body = {
        "model": NATIVE_MODEL,
        "max_tokens": 50,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    collected = b""
    async with anthropic_test_client.stream(
        "POST", "/v1/messages", json=body, headers=_auth()
    ) as r:
        assert r.status_code == 200
        async for raw in r.aiter_bytes():
            collected += raw

    assert b": keepalive" in collected  # heartbeats before giving up
    assert b"event: error" in collected  # abort surfaced to the client
    assert captured.get("status_code") == 504
    assert captured.get("error") is not None
    assert captured["usage"]["input_tokens"] == 0  # failed stream -> not billed
    assert captured["usage"]["output_tokens"] == 0


# ---------------------------------------------------------------------------
# Error-mapping + count_tokens correctness fixes
# ---------------------------------------------------------------------------


def test_map_upstream_status_remaps_auth_and_preserves_ratelimit():
    """C4/C6: upstream 401/403 -> 502 api_error; 429/503 keep their Anthropic type."""
    from serving.servers.routers.anthropic_messages import _map_upstream_status

    assert _map_upstream_status(401) == (502, "api_error")
    assert _map_upstream_status(403) == (502, "api_error")
    assert _map_upstream_status(429) == (429, "rate_limit_error")
    assert _map_upstream_status(503) == (503, "overloaded_error")
    assert _map_upstream_status(500) == (500, "api_error")


def _client_response_error(status: int, message: str = "err"):
    from types import SimpleNamespace

    import aiohttp
    from yarl import URL

    return aiohttp.ClientResponseError(
        request_info=SimpleNamespace(real_url=URL("http://upstream.test")),
        history=(),
        status=status,
        message=message,
    )


@pytest.mark.asyncio
async def test_upstream_401_remapped_to_502_not_401(anthropic_test_client, monkeypatch):
    """C6: an upstream 401 (operator key bad) must not become a client 401."""

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        raise _client_response_error(401, "Incorrect API key provided")

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    body = {
        "model": OPENAI_MODEL,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 502
    err = r.json()
    assert err["error"]["type"] == "api_error"
    # The provider's own auth message must not leak to the user.
    assert "Incorrect API key" not in err["error"]["message"]


@pytest.mark.asyncio
async def test_key_pool_exhausted_returns_429_rate_limit(anthropic_test_client, monkeypatch):
    """C7: all keys muted -> 429 rate_limit_error, not a generic 502."""
    from serving.adapters.key_pool import KeyPoolExhausted

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        raise KeyPoolExhausted("No active API keys")

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    body = {
        "model": OPENAI_MODEL,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 429
    err = r.json()
    assert err["error"]["type"] == "rate_limit_error"
    # retry-after matches the key-pool mute window so clients don't retry early.
    assert r.headers.get("retry-after") == "300"


@pytest.mark.asyncio
async def test_dict_detail_error_wrapped_in_anthropic_envelope():
    """C8: a pre-shaped dict error on the Anthropic surface gets the Anthropic envelope."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from serving.servers.routers.anthropic_messages import (
        anthropic_aware_http_exception_handler,
    )

    request = SimpleNamespace(url=SimpleNamespace(path="/v1/messages"), method="POST")
    exc = HTTPException(
        status_code=429,
        detail={"error": {"code": "concurrency_limit_exceeded", "message": "Too many", "limit": 5}},
    )
    resp = await anthropic_aware_http_exception_handler(request, exc)
    import json as _json

    payload = _json.loads(bytes(resp.body))
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "rate_limit_error"
    assert payload["error"]["message"] == "Too many"


@pytest.mark.asyncio
async def test_count_tokens_returns_input_tokens(anthropic_test_client):
    """C3: POST /v1/messages/count_tokens is implemented and Anthropic-shaped."""
    body = {
        "model": OPENAI_MODEL,
        "system": [{"type": "text", "text": "You are helpful."}],
        "messages": [{"role": "user", "content": "Count the tokens in this sentence please."}],
    }
    r = await anthropic_test_client.post("/v1/messages/count_tokens", json=body, headers=_auth())
    assert r.status_code == 200
    out = r.json()
    assert isinstance(out["input_tokens"], int)
    assert out["input_tokens"] > 0


@pytest.mark.asyncio
async def test_count_tokens_requires_model(anthropic_test_client):
    """C3: missing model -> Anthropic-shaped 400, not a bare 404."""
    r = await anthropic_test_client.post(
        "/v1/messages/count_tokens",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(),
    )
    assert r.status_code == 400
    assert r.json()["type"] == "error"


@pytest.mark.asyncio
async def test_count_tokens_unknown_model_returns_404(anthropic_test_client):
    """count_tokens enforces model visibility, like /v1/messages (no probing hidden models)."""
    r = await anthropic_test_client.post(
        "/v1/messages/count_tokens",
        json={"model": "no-such-model", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(),
    )
    assert r.status_code == 404
    err = r.json()
    assert err["type"] == "error"
    assert err["error"]["type"] == "not_found_error"


@pytest.mark.asyncio
async def test_user_balance_returns_remaining_quota(anthropic_test_client):
    """GET /anthropic/user/balance reports the daily quota as a balance."""
    r = await anthropic_test_client.get("/anthropic/user/balance", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["is_available"] is True
    assert body["currency"] == "USD"
    assert body["daily_limit_usd"] == 100.0
    assert body["spent_today_usd"] == 0.0
    assert body["balance_usd"] == 100.0
    assert body["reset_at"] is not None


@pytest.mark.asyncio
async def test_user_balance_reflects_spend(anthropic_test_client):
    """Balance drops with reported spend, and never goes negative."""
    from serving.servers.auth import verify_api_key_for_balance

    app = anthropic_test_client._transport.app
    app.dependency_overrides[verify_api_key_for_balance] = lambda: {
        "authenticated": True,
        "user_id": "test-user",
        "quota_daily_cost_usd": 10.0,
        "spent_today_usd": 12.5,
    }

    r = await anthropic_test_client.get("/anthropic/user/balance", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["is_available"] is False
    assert body["balance_usd"] == 0.0
    assert body["spent_today_usd"] == 12.5


@pytest.mark.asyncio
async def test_user_balance_tiny_remainder_rounds_to_unavailable(anthropic_test_client):
    """A sub-rounding-precision remainder must not claim is_available while balance_usd shows 0.0."""
    from serving.servers.auth import verify_api_key_for_balance

    app = anthropic_test_client._transport.app
    app.dependency_overrides[verify_api_key_for_balance] = lambda: {
        "authenticated": True,
        "user_id": "test-user",
        "quota_daily_cost_usd": 10.0,
        "spent_today_usd": 9.99999,
    }

    r = await anthropic_test_client.get("/anthropic/user/balance", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["balance_usd"] == 0.0
    assert body["is_available"] is False


@pytest.mark.asyncio
async def test_user_balance_exhausted_quota_does_not_401_or_429(anthropic_test_client):
    """Checking balance must keep working exactly when quota is exhausted."""
    from serving.servers.auth import verify_api_key_for_balance

    app = anthropic_test_client._transport.app
    app.dependency_overrides[verify_api_key_for_balance] = lambda: {
        "authenticated": True,
        "user_id": "test-user",
        "quota_daily_cost_usd": 5.0,
        "spent_today_usd": 5.0,
    }

    r = await anthropic_test_client.get("/anthropic/user/balance", headers=_auth())
    assert r.status_code == 200
    assert r.json()["is_available"] is False


@pytest.mark.asyncio
async def test_user_balance_invalid_key_returns_anthropic_format_401(anthropic_test_client):
    r = await anthropic_test_client.get(
        "/anthropic/user/balance", headers={"x-api-key": "hyi-wrong"}
    )
    assert r.status_code == 401
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "authentication_error"


def test_map_upstream_status_remaps_402_billing():
    """402 (upstream out of credit) must not surface as the client's payment failure."""
    from serving.servers.routers.anthropic_messages import _map_upstream_status

    assert _map_upstream_status(402) == (502, "api_error")


@pytest.mark.asyncio
async def test_unimplemented_v1_messages_path_returns_anthropic_shaped_404():
    """P9: a router 404 (Starlette-raised) under /v1/messages gets the Anthropic envelope."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from serving.servers.routers import anthropic_messages

    app = FastAPI()
    # Mirror app.py: register the Anthropic-aware handler on the Starlette base
    # class so router-raised 404/405 are matched (fastapi.HTTPException would not).
    app.add_exception_handler(
        StarletteHTTPException, anthropic_messages.anthropic_aware_http_exception_handler
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/v1/messages/batches", json={})
    assert r.status_code == 404
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "not_found_error"
