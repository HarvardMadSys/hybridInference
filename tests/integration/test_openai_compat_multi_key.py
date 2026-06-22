"""Integration tests for multi-key rotation in OpenAICompatAdapter.

Covers:
- 429 on key K mutes K and the next call goes to a different key.
- Other key-specific / transient errors (e.g. 500) and network errors also
  mute K and rotate.
- Request-scoped client errors (e.g. 400) propagate without muting or rotating.
- All keys erroring in one call propagates the last error.
- Single-`api_key` routes do NOT create a key pool (legacy path).

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
        provider="zai",
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

    with (
        patch.object(adapter.http, "json_post", side_effect=always_429),
        pytest.raises(aiohttp.ClientResponseError) as exc_info,
    ):
        await adapter.chat_completion([{"role": "user", "content": "hi"}])

    assert exc_info.value.status == 429


async def test_single_api_key_legacy_path_unchanged():
    """Routes with `api_key` (no `api_keys`) do not create a pool."""
    config = ModelConfig(
        id="test-model",
        name="test-model",
        provider="zai",
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


async def test_multi_key_rotates_on_non_429_error():
    """First key 500s, second key returns 200; the 500 mutes only the first key."""
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
            raise _make_response_error(500)
        assert headers["Authorization"] == "Bearer k2"
        return success_payload

    with patch.object(adapter.http, "json_post", side_effect=fake_json_post):
        result = await adapter.chat_completion([{"role": "user", "content": "hi"}])

    assert call_count["n"] == 2
    assert "choices" in result
    # k1 was muted by the 500; k2 served the request and stayed clean.
    assert adapter._key_pool is not None
    assert adapter._key_pool._keys[0].cooldown_until > 0
    assert adapter._key_pool._keys[1].cooldown_until == 0


async def test_multi_key_rotates_on_network_error():
    """A connection error (no HTTP status) mutes the key and rotates."""
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
            raise aiohttp.ServerDisconnectedError("connection reset")
        assert headers["Authorization"] == "Bearer k2"
        return success_payload

    with patch.object(adapter.http, "json_post", side_effect=fake_json_post):
        result = await adapter.chat_completion([{"role": "user", "content": "hi"}])

    assert call_count["n"] == 2
    assert "choices" in result
    assert adapter._key_pool is not None
    assert adapter._key_pool._keys[0].cooldown_until > 0
    assert adapter._key_pool._keys[1].cooldown_until == 0


async def test_request_scoped_4xx_propagates_without_muting():
    """A 400 fails fast: it does not mute the key or rotate to the others."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))

    call_count = {"n": 0}

    async def bad_request(url, json, headers, timeout):
        call_count["n"] += 1
        raise _make_response_error(400)

    with (
        patch.object(adapter.http, "json_post", side_effect=bad_request),
        pytest.raises(aiohttp.ClientResponseError) as exc_info,
    ):
        await adapter.chat_completion([{"role": "user", "content": "hi"}])

    assert exc_info.value.status == 400
    # Only the first key was tried; no rotation, no muting.
    assert call_count["n"] == 1
    assert adapter._key_pool is not None
    assert adapter._key_pool._keys[0].cooldown_until == 0
    assert adapter._key_pool._keys[1].cooldown_until == 0


async def test_all_keys_error_propagates_last_error():
    """Every key 500s in one call -> all keys muted, the last 500 propagates."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))

    async def always_500(url, json, headers, timeout):
        raise _make_response_error(500)

    with (
        patch.object(adapter.http, "json_post", side_effect=always_500),
        pytest.raises(aiohttp.ClientResponseError) as exc_info,
    ):
        await adapter.chat_completion([{"role": "user", "content": "hi"}])

    assert exc_info.value.status == 500
    # Both keys entered cooldown.
    assert adapter._key_pool is not None
    assert adapter._key_pool._keys[0].cooldown_until > 0
    assert adapter._key_pool._keys[1].cooldown_until > 0


def _make_stream_gen(
    *,
    status: int | None = None,
    retry_after: str | None = None,
    chunks: tuple[str, ...] = (),
):
    """Return a fresh async generator that mocks ``http.stream_post``.

    If ``status`` is set, the generator raises ``ClientResponseError`` on the
    first ``__anext__`` (mirrors how ``_open_stream_with_pool`` detects opening
    429s before any chunk has been yielded). Otherwise it yields ``chunks``.
    """

    async def gen():
        if status is not None:
            raise _make_response_error(status, retry_after=retry_after)
        else:
            # Reachable yield keeps `gen` an async generator so the error case
            # surfaces on the first ``__anext__`` call rather than at construction.
            for c in chunks:
                yield c

    return gen()


async def test_streaming_rotates_on_429_at_open():
    """First key 429s before yielding any chunk; helper rotates to second key."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))

    sse_chunks = (
        'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        "data: [DONE]\n\n",
    )

    call_count = {"n": 0}

    def stream_side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _make_stream_gen(status=429, retry_after="1")
        return _make_stream_gen(chunks=sse_chunks)

    with patch.object(adapter.http, "stream_post", side_effect=stream_side_effect) as mock_stream:
        collected: list[str] = []
        async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "hi"}]):
            collected.append(chunk)

    assert call_count["n"] == 2
    # Verify the auth header rotated between the two attempts.
    assert mock_stream.call_args_list[0].kwargs["headers"]["Authorization"] == "Bearer k1"
    assert mock_stream.call_args_list[1].kwargs["headers"]["Authorization"] == "Bearer k2"

    # k1 is in cooldown; k2 is clean.
    assert adapter._key_pool is not None
    assert adapter._key_pool._keys[0].cooldown_until > 0
    assert adapter._key_pool._keys[1].cooldown_until == 0

    # The consumer received non-empty content somewhere in the stream.
    assert collected, "expected at least one streamed chunk"
    assert any("hi" in c for c in collected)


async def test_streaming_pool_exhausted_propagates():
    """Every key 429s on stream open → final 429 propagates to the caller."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))

    def always_429(*args, **kwargs):
        return _make_stream_gen(status=429, retry_after="1")

    with (
        patch.object(adapter.http, "stream_post", side_effect=always_429),
        pytest.raises(aiohttp.ClientResponseError) as exc_info,
    ):
        async for _ in adapter.stream_chat_completion([{"role": "user", "content": "hi"}]):
            pass  # pragma: no cover — generator is expected to raise before yielding

    assert exc_info.value.status == 429
    # Both keys cooled down.
    assert adapter._key_pool is not None
    assert adapter._key_pool._keys[0].cooldown_until > 0
    assert adapter._key_pool._keys[1].cooldown_until > 0


async def test_streaming_mid_stream_error_mutes_key():
    """An error after the first chunk mutes the key via the error-status release."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))

    async def gen():
        yield 'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        yield 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        raise aiohttp.ServerDisconnectedError("mid-stream drop")

    with (
        patch.object(adapter.http, "stream_post", return_value=gen()),
        pytest.raises(aiohttp.ServerDisconnectedError),
    ):
        async for _ in adapter.stream_chat_completion([{"role": "user", "content": "hi"}]):
            pass

    # The committed key (k1) was muted by the mid-stream failure.
    assert adapter._key_pool is not None
    assert adapter._key_pool._keys[0].cooldown_until > 0


async def test_streaming_processor_error_does_not_mute_key():
    """An adapter-side chunk-processing error is not key-specific, so no mute."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))

    sse_chunks = (
        'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        "data: [DONE]\n\n",
    )

    class _BoomProcessor:
        def process_stream_chunk(self, data):
            raise ValueError("bad chunk")

        def flush(self):
            return []

    with (
        patch.object(
            adapter.http,
            "stream_post",
            side_effect=lambda *a, **k: _make_stream_gen(chunks=sse_chunks),
        ),
        patch(
            "serving.adapters.openai_compat.get_processor",
            return_value=_BoomProcessor(),
        ),
        pytest.raises(ValueError),
    ):
        async for _ in adapter.stream_chat_completion([{"role": "user", "content": "hi"}]):
            pass

    # The processing error propagated, but the key was not muted.
    assert adapter._key_pool is not None
    assert adapter._key_pool._keys[0].cooldown_until == 0


async def test_streaming_client_disconnect_does_not_mute_key():
    """Client closing the stream early (GeneratorExit) must not mute the key."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))

    sse_chunks = (
        'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":" there"}}]}\n\n',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        "data: [DONE]\n\n",
    )

    with patch.object(
        adapter.http, "stream_post", side_effect=lambda *a, **k: _make_stream_gen(chunks=sse_chunks)
    ):
        agen = adapter.stream_chat_completion([{"role": "user", "content": "hi"}])
        # Pull the first chunk, then close the generator (simulated disconnect).
        await agen.__anext__()
        await agen.aclose()

    # A client-side cancellation is not an upstream error — k1 stays usable.
    assert adapter._key_pool is not None
    assert adapter._key_pool._keys[0].cooldown_until == 0
