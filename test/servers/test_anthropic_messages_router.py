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
        "id": "msg_native", "type": "message", "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "Hi"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 1},
    }

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        return upstream_resp

    from serving.http import AsyncHTTPClient
    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    body = {"model": NATIVE_MODEL, "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}]}
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 200
    assert r.json() == upstream_resp


@pytest.mark.asyncio
async def test_anthropic_v1_messages_alias_reaches_same_handler(anthropic_test_client, monkeypatch):
    upstream_resp = {
        "id": "msg_alias", "type": "message", "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "via alias"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        return upstream_resp

    from serving.http import AsyncHTTPClient
    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    body = {"model": NATIVE_MODEL, "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}]}
    r = await anthropic_test_client.post("/anthropic/v1/messages", json=body, headers=_auth())
    assert r.status_code == 200
    assert r.json()["content"][0]["text"] == "via alias"


@pytest.mark.asyncio
async def test_openai_backend_translated(anthropic_test_client, monkeypatch):
    """Anthropic-format -> glm-4.7 (zhipu) -> translation."""
    openai_resp = {
        "id": "chatcmpl-1", "object": "chat.completion",
        "model": OPENAI_MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Translated reply"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
    }

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        return openai_resp

    from serving.http import AsyncHTTPClient
    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    body = {"model": OPENAI_MODEL, "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}]}
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 200
    out = r.json()
    assert out["type"] == "message"
    assert out["content"][0]["text"] == "Translated reply"
    assert out["usage"]["input_tokens"] == 9


@pytest.mark.asyncio
async def test_alias_model_id_resolves(anthropic_test_client, monkeypatch):
    upstream_resp = {
        "id": "msg_a", "type": "message", "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
    }

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        return upstream_resp

    from serving.http import AsyncHTTPClient
    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    body = {"model": "claude-3-opus-latest", "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}]}
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_unknown_model_returns_anthropic_format_404(anthropic_test_client):
    body = {"model": "no-such-model", "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}]}
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 404
    err = r.json()
    assert err["type"] == "error"
    assert err["error"]["type"] == "not_found_error"
