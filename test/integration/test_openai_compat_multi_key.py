"""Integration tests for multi-key rotation in OpenAICompatAdapter.

Covers:
- 429 on key K cools K down and the next call goes to a different key.
- All keys returning 429 in one call propagates the last 429.
- Single-`api_key` routes do NOT create a key pool (legacy path).
- Non-429 errors (e.g. 500) never put a key in cooldown.

The pool integration routes through ``OpenAICompatAdapter._post_with_pool``,
which calls ``self.http.json_post`` (NOT ``json_post_with_retry``) when a pool
is configured. The legacy single-key path uses ``json_post_with_retry``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter


def _make_config(api_keys: list[str]) -> ModelConfig:
    """Build a ModelConfig wired for multi-key rotation."""
    return ModelConfig(
        id="test-model",
        name="test-model",
        provider="zhipu",
        base_url="https://api.example.com",
        api_keys=api_keys,
        provider_model_id="test-model",
    )


def _make_response_error(
    status: int, retry_after: str | None = None
) -> aiohttp.ClientResponseError:
    """Build a minimal aiohttp.ClientResponseError suitable for the adapter."""
    headers: dict[str, str] = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return aiohttp.ClientResponseError(
        request_info=AsyncMock(real_url="https://api.example.com"),
        history=(),
        status=status,
        message="rate limited" if status == 429 else "upstream error",
        headers=headers,
    )


async def test_multi_key_rotates_on_429():
    """First key gets 429, second key returns 200; user gets the 200."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))

    success_payload = {
        "id": "x",
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        "usage": {},
    }

    call_count = {"n": 0}

    async def fake_json_post(url, json, headers, timeout):
        call_count["n"] += 1
        if call_count["n"] == 1:
            assert headers["Authorization"] == "Bearer k1"
            raise _make_response_error(429, retry_after="1")
        assert headers["Authorization"] == "Bearer k2"
        return success_payload

    with patch.object(adapter.http, "json_post", side_effect=fake_json_post):
        result = await adapter.chat_completion([{"role": "user", "content": "hi"}])

    assert call_count["n"] == 2
    assert "choices" in result
    # k1 went into cooldown, k2 did not.
    assert adapter._key_pool is not None
    assert adapter._key_pool._keys[0].cooldown_until > 0
    assert adapter._key_pool._keys[1].cooldown_until == 0


async def test_multi_key_pool_exhausted_propagates():
    """All keys 429 in one call -> final exception is the last 429."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))

    async def always_429(url, json, headers, timeout):
        raise _make_response_error(429, retry_after="1")

    with patch.object(adapter.http, "json_post", side_effect=always_429):
        with pytest.raises(aiohttp.ClientResponseError) as exc_info:
            await adapter.chat_completion([{"role": "user", "content": "hi"}])

    assert exc_info.value.status == 429


async def test_single_api_key_legacy_path_unchanged():
    """Routes with `api_key` (no `api_keys`) do not create a pool."""
    config = ModelConfig(
        id="test-model",
        name="test-model",
        provider="zhipu",
        base_url="https://api.example.com",
        api_key="single-key",
        provider_model_id="test-model",
    )
    adapter = OpenAICompatAdapter(config)
    assert adapter._key_pool is None

    success_payload = {
        "id": "x",
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        "usage": {},
    }

    captured: dict[str, dict[str, str]] = {}

    async def ok(url, json, headers, timeout, retries=2):
        captured["headers"] = headers
        return success_payload

    # Legacy single-key path uses json_post_with_retry.
    with patch.object(adapter.http, "json_post_with_retry", side_effect=ok) as mock_legacy:
        result = await adapter.chat_completion([{"role": "user", "content": "hi"}])

    assert "choices" in result
    assert mock_legacy.await_count == 1
    assert captured["headers"]["Authorization"] == "Bearer single-key"


async def test_non_429_error_does_not_cooldown():
    """A 500 error must not place a key in cooldown."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))

    async def server_err(url, json, headers, timeout):
        raise _make_response_error(500)

    with patch.object(adapter.http, "json_post", side_effect=server_err):
        with pytest.raises(aiohttp.ClientResponseError):
            await adapter.chat_completion([{"role": "user", "content": "hi"}])

    # Neither key entered cooldown.
    assert adapter._key_pool is not None
    assert adapter._key_pool._keys[0].cooldown_until == 0
    assert adapter._key_pool._keys[1].cooldown_until == 0
