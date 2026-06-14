"""Tests for Gemini adapter usage parsing edge cases."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.gemini import GeminiAdapter


def _make_adapter(response_payload):
    config = ModelConfig(
        id="gemini-test",
        name="Gemini Test",
        provider="gemini",
        base_url="https://mock",
        api_key="test-key",
    )
    adapter = GeminiAdapter(config)
    adapter.http = MagicMock()
    adapter.http.json_post_with_retry = AsyncMock(return_value=response_payload)
    return adapter


def _simple_messages():
    return [{"role": "user", "content": "Hello"}]


@pytest.mark.asyncio
async def test_gemini_usage_normal_with_all_fields(monkeypatch):
    payload = {
        "candidates": [{"content": {"parts": [{"text": "Hello"}]}}],
        "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 50,
            "totalTokenCount": 150,
            "thoughtsTokenCount": 0,
            "cachedContentTokenCount": 20,
        },
    }

    adapter = _make_adapter(payload)
    response = await adapter.chat_completion(_simple_messages())

    usage = response["usage"]
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 50
    assert usage["total_tokens"] == 150
    assert usage["cache_read_tokens"] == 20


@pytest.mark.asyncio
async def test_gemini_usage_missing_candidates_token_count(monkeypatch):
    payload = {
        "candidates": [{"content": {"parts": [{"text": "Hello"}]}}],
        "usageMetadata": {
            "promptTokenCount": 100,
            "totalTokenCount": 150,
            "thoughtsTokenCount": 10,
        },
    }

    adapter = _make_adapter(payload)
    response = await adapter.chat_completion(_simple_messages())

    usage = response["usage"]
    assert usage["completion_tokens"] == 40
    assert usage["total_tokens"] == 150


@pytest.mark.asyncio
async def test_gemini_usage_zero_total_with_content(monkeypatch):
    monkeypatch.setattr("serving.adapters.gemini.estimate_text_tokens", lambda _: 12)
    payload = {
        "candidates": [{"content": {"parts": [{"text": "Hello world"}]}}],
        "usageMetadata": {
            "promptTokenCount": 100,
            "totalTokenCount": 0,
            "thoughtsTokenCount": 0,
        },
    }

    adapter = _make_adapter(payload)
    response = await adapter.chat_completion(_simple_messages())

    usage = response["usage"]
    assert usage["completion_tokens"] == 12
    assert usage["total_tokens"] == 112


@pytest.mark.asyncio
async def test_gemini_usage_negative_calculated(monkeypatch):
    monkeypatch.setattr("serving.adapters.gemini.estimate_text_tokens", lambda _: 7)
    payload = {
        "candidates": [{"content": {"parts": [{"text": "Hi"}]}}],
        "usageMetadata": {
            "promptTokenCount": 200,
            "totalTokenCount": 150,
            "thoughtsTokenCount": 0,
        },
    }

    adapter = _make_adapter(payload)
    response = await adapter.chat_completion(_simple_messages())

    usage = response["usage"]
    assert usage["completion_tokens"] == 7
    assert usage["total_tokens"] == 150


@pytest.mark.asyncio
async def test_gemini_usage_type_safety(monkeypatch):
    payload = {
        "candidates": [{"content": {"parts": [{"text": "Hi"}]}}],
        "usageMetadata": {
            "promptTokenCount": "100",
            "candidatesTokenCount": "50",
            "totalTokenCount": "150",
            "cachedContentTokenCount": "30",
        },
    }

    adapter = _make_adapter(payload)
    response = await adapter.chat_completion(_simple_messages())

    usage = response["usage"]
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 50
    assert usage["total_tokens"] == 150
    assert usage["cache_read_tokens"] == 30


@pytest.mark.asyncio
async def test_gemini_stream_explicit_zero_cache_emits_cache_read():
    """Gemini streaming with cachedContentTokenCount: 0 must emit cache_read_tokens: 0.

    A reported zero is a real provider signal; dropping the field downstream would
    make it indistinguishable from a provider that did not report cache usage.
    """
    lines = [
        json.dumps({"candidates": [{"content": {"parts": [{"text": "Hi"}]}}]}),
        json.dumps(
            {
                "candidates": [{"content": {"parts": []}, "finishReason": "STOP"}],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 15,
                    "cachedContentTokenCount": 0,
                },
            }
        ),
    ]

    async def _fake_stream(*_args, **_kwargs):
        for line in lines:
            yield line

    config = ModelConfig(
        id="gemini-test",
        name="Gemini Test",
        provider="gemini",
        base_url="https://mock",
        api_key="test-key",
    )
    adapter = GeminiAdapter(config)
    adapter.http = MagicMock()
    adapter.http.stream_post = MagicMock(return_value=_fake_stream())

    usage = None
    async for chunk in adapter.stream_chat_completion(_simple_messages()):
        raw = chunk[6:] if chunk.startswith("data: ") else chunk
        raw = raw.strip()
        if not raw or raw == "[DONE]":
            continue
        obj = json.loads(raw)
        if "usage" in obj:
            usage = obj["usage"]

    assert usage is not None
    assert usage["cache_read_tokens"] == 0
