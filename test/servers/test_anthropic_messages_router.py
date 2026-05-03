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
    into the DB and must fold cache_read/cache_creation tokens into
    prompt_tokens / total_tokens (OpenAI-style semantics expected by every
    downstream report)."""
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
    body = {"model": NATIVE_MODEL, "max_tokens": 50, "messages": messages}
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 200

    await __import__("asyncio").wait_for(captured_event.wait(), timeout=2.0)

    assert captured["prompt"] == messages
    assert captured["response"] == upstream_resp
    usage = captured["usage"]
    # input_tokens=7 + cache_read=100 + cache_write=50 -> prompt_tokens=157
    assert usage["prompt_tokens"] == 157
    assert usage["completion_tokens"] == 3
    assert usage["total_tokens"] == 160
    assert usage["cache_read_tokens"] == 100
    assert usage["cache_write_tokens"] == 50


@pytest.mark.asyncio
async def test_streaming_logs_prompt_and_cache_inclusive_tokens(anthropic_test_client, monkeypatch):
    """Streaming path must persist the request prompt and fold cache tokens
    into prompt_tokens / total_tokens. Response stays None on streaming
    (matches the OpenAI streaming logging contract)."""
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
    assert captured["response"] is None
    usage = captured["usage"]
    # input=4 + cache_read=20 + cache_write=10 = 34
    assert usage["prompt_tokens"] == 34
    assert usage["completion_tokens"] == 2
    assert usage["total_tokens"] == 36
    assert usage["cache_read_tokens"] == 20
    assert usage["cache_write_tokens"] == 10
