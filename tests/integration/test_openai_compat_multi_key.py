"""Integration tests for multi-key rotation in OpenAICompatAdapter.

Covers:
- 429 on key K rotates the call onto a different key, leaving K unmuted while
  another key was still untried.
- Other key-specific / transient errors (e.g. 500) and network errors rotate
  the same way.
- Request-scoped client errors (e.g. 400) propagate without rotating or muting.
- All keys erroring in one call propagates the last error, and mutes the last
  key tried — the one with nowhere left to rotate to.
- Single-`api_key` routes do NOT create a key pool (legacy path).

The pool integration routes through ``OpenAICompatAdapter._post_with_pool``,
which calls ``self.http.json_post`` (NOT ``json_post_with_retry``) when a pool
is configured. The legacy single-key path uses ``json_post_with_retry``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.adapters.upstream_limiter import UpstreamSlot
from serving.utils import context as req_ctx


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
    """First key gets 429, second key returns 200; user gets the 200.

    Rotation precedes muting, so k1 keeps its place in the pool: another key was
    untried, so the 429 costs this request a second attempt and nothing more.
    """
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

    # Keep this test independent of request context left by earlier tests on the
    # same xdist worker. This path intentionally models an anonymous caller.
    with (
        req_ctx.push(affinity_key="_anon", user_role=None),
        patch.object(adapter.http, "json_post", side_effect=fake_json_post),
    ):
        result = await adapter.chat_completion([{"role": "user", "content": "hi"}])

    assert call_count["n"] == 2
    assert "choices" in result
    # Neither key is in cooldown: k1 was rotated past, not muted.
    assert adapter._key_pool is not None
    assert adapter._key_pool._keys[0].cooldown_until == 0
    assert adapter._key_pool._keys[1].cooldown_until == 0
    # The caller's affinity followed the rotation onto the key that worked, so
    # its next request goes straight to k2 instead of paying for k1 again.
    assert adapter._key_pool._affinity["_anon"].key_index == 1


async def test_non_affine_failover_advances_without_shared_affinity():
    """An unresolved caller remembers failover state without a shared binding."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))
    success_payload = {
        "id": "x",
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        "usage": {},
    }
    used: list[str] = []

    async def fake_json_post(url, json, headers, timeout):
        key = headers["Authorization"].removeprefix("Bearer ")
        used.append(key)
        if key == "k1":
            raise _make_response_error(503)
        return success_payload

    with patch.object(adapter.http, "json_post", side_effect=fake_json_post):
        with req_ctx.push(affinity_key=None, auth_key_hash="_anon"):
            await adapter.chat_completion([{"role": "user", "content": "hi"}])
        with req_ctx.push(affinity_key=None, auth_key_hash="_anon"):
            await adapter.chat_completion([{"role": "user", "content": "hi"}])

    assert used == ["k1", "k2", "k2"]
    assert adapter._key_pool is not None
    assert adapter._key_pool.affinity_count() == 0


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


async def test_add_runtime_key_promotes_single_key_adapter():
    """A runtime key promotes a single-`api_key` adapter to a pool.

    The original static key is seeded alongside the new key so both keep
    serving traffic, and the request path switches to the pool without a
    restart.
    """
    config = ModelConfig(
        id="test-model",
        name="test-model",
        provider="minimax",
        base_url="https://api.example.com",
        api_key="env-key",
        provider_model_id="test-model",
    )
    adapter = OpenAICompatAdapter(config)
    assert adapter._key_pool is None

    assert adapter.add_runtime_key("dash-key") is True
    assert adapter._key_pool is not None
    assert sorted(adapter._key_pool.snapshot_keys()) == ["dash-key", "env-key"]

    # A second runtime key is appended to the now-existing pool.
    assert adapter.add_runtime_key("dash-key-2") is True
    assert sorted(adapter._key_pool.snapshot_keys()) == [
        "dash-key",
        "dash-key-2",
        "env-key",
    ]

    # Blank keys are ignored.
    assert adapter.add_runtime_key("   ") is False


async def test_add_runtime_key_without_static_key_seeds_pool():
    """Promotion works even when the route had no static api_key."""
    config = ModelConfig(
        id="test-model",
        name="test-model",
        provider="minimax",
        base_url="https://api.example.com",
        provider_model_id="test-model",
    )
    adapter = OpenAICompatAdapter(config)
    assert adapter._key_pool is None

    assert adapter.add_runtime_key("dash-key") is True
    assert adapter._key_pool is not None
    assert adapter._key_pool.snapshot_keys() == ["dash-key"]


async def test_empty_pool_raises_keypool_exhausted_not_assertion():
    """A pool drained to size 0 yields KeyPoolExhausted, not an AssertionError.

    Happens when the only static key was disabled (tombstone) and there are no
    DB keys: the adapter is promoted to a pool that is then emptied. The request
    path must surface a controlled error for router fallback.
    """
    from serving.adapters.key_pool import KeyPoolExhausted

    adapter = OpenAICompatAdapter(_make_config(["only-key"]))
    # Drain the pool so size() == 0, mirroring tombstone removal at boot.
    adapter._key_pool.remove_key("only-key")
    assert adapter._key_pool.size() == 0

    with pytest.raises(KeyPoolExhausted):
        await adapter.chat_completion([{"role": "user", "content": "hi"}])


async def test_multi_key_rotates_on_non_429_error():
    """First key 500s, second key returns 200; the 500 only moves the request."""
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
    # k1 was rotated past, not muted; k2 served the request.
    assert adapter._key_pool is not None
    assert adapter._key_pool._keys[0].cooldown_until == 0
    assert adapter._key_pool._keys[1].cooldown_until == 0


async def test_multi_key_rotates_on_network_error():
    """A connection error (no HTTP status) rotates to the next key."""
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
    assert adapter._key_pool._keys[0].cooldown_until == 0
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


async def test_all_keys_500_mutes_only_the_key_with_nowhere_left_to_go():
    """Every key 500s (transient) -> only the last key tried mutes; the 500 propagates."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))

    async def always_500(url, json, headers, timeout):
        raise _make_response_error(500)

    with (
        patch.object(adapter.http, "json_post", side_effect=always_500),
        pytest.raises(aiohttp.ClientResponseError) as exc_info,
    ):
        await adapter.chat_completion([{"role": "user", "content": "hi"}])

    assert exc_info.value.status == 500
    # k1's failure was absorbed by rotating onto k2. k2 had nowhere left to go,
    # so it took the mute — and k1, still usable, keeps the route alive, which
    # is what the transient-error guard exists to protect.
    assert adapter._key_pool is not None
    assert adapter._key_pool._keys[0].cooldown_until == 0
    assert adapter._key_pool._keys[1].cooldown_until > 0


async def test_all_keys_429_mute_entire_pool():
    """Every key 429s (key-specific) -> eventually all keys muted, including the last.

    The first call rotates k1 -> k2 and mutes only k2, the key it ran out of
    alternatives on. k1 is then the *sole* usable key, so it gets
    ``SOLE_KEY_BACKOFF_THRESHOLD`` free passes (429 propagates but k1 stays
    usable) before it too mutes — there's nowhere left to rotate to, so a
    single blip on the last key must not cost the full mute duration.
    """
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))
    assert adapter._key_pool is not None

    async def always_429(url, json, headers, timeout):
        raise _make_response_error(429, retry_after="1")

    with patch.object(adapter.http, "json_post", side_effect=always_429):
        # k2's mute (call 1, after the rotation) plus k1's free passes and final
        # mute together take SOLE_KEY_BACKOFF_THRESHOLD + 1 calls.
        for _ in range(adapter._key_pool.SOLE_KEY_BACKOFF_THRESHOLD + 1):
            with pytest.raises(aiohttp.ClientResponseError) as exc_info:
                await adapter.chat_completion([{"role": "user", "content": "hi"}])
            assert exc_info.value.status == 429

    assert adapter._key_pool._keys[0].cooldown_until > 0
    assert adapter._key_pool._keys[1].cooldown_until > 0


async def test_single_key_transient_error_does_not_mute():
    """A transient 5xx on a sole-key pool must not take the route offline."""
    adapter = OpenAICompatAdapter(_make_config(["only"]))

    async def always_500(url, json, headers, timeout):
        raise _make_response_error(500)

    with (
        patch.object(adapter.http, "json_post", side_effect=always_500),
        pytest.raises(aiohttp.ClientResponseError) as exc_info,
    ):
        await adapter.chat_completion([{"role": "user", "content": "hi"}])

    assert exc_info.value.status == 500
    # The sole key stays usable so the next request still reaches the provider.
    assert adapter._key_pool is not None
    assert adapter._key_pool._keys[0].cooldown_until == 0


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

    # Neither key is in cooldown: the open error rotated past k1, it did not
    # mute it — k2 was untried and took the stream.
    assert adapter._key_pool is not None
    assert adapter._key_pool._keys[0].cooldown_until == 0
    assert adapter._key_pool._keys[1].cooldown_until == 0

    # The consumer received non-empty content somewhere in the stream.
    assert collected, "expected at least one streamed chunk"
    assert any("hi" in c for c in collected)


async def test_streaming_open_io_error_does_not_replay_on_another_key():
    """A non-status I/O failure while opening must not re-submit on another key.

    The upstream may have returned 2xx and started streaming before the drop;
    rotating would risk duplicate generation / double billing, so the error
    propagates after a single attempt.
    """
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))

    call_count = {"n": 0}

    def stream_side_effect(*args, **kwargs):
        call_count["n"] += 1

        async def gen():
            raise aiohttp.ServerDisconnectedError("body drop after 2xx")
            yield  # pragma: no cover — makes this an async generator

        return gen()

    with (
        patch.object(adapter.http, "stream_post", side_effect=stream_side_effect),
        pytest.raises(aiohttp.ServerDisconnectedError),
    ):
        async for _ in adapter.stream_chat_completion([{"role": "user", "content": "hi"}]):
            pass

    # Only one upstream attempt — no replay on k2.
    assert call_count["n"] == 1
    assert adapter._key_pool is not None
    assert adapter._key_pool._keys[0].cooldown_until == 0
    assert adapter._key_pool._keys[1].cooldown_until == 0


async def test_streaming_pool_exhausted_propagates():
    """Every key 429s on stream open → final 429 propagates to the caller.

    Same sole-key free-pass behavior as test_all_keys_429_mute_entire_pool:
    the first call rotates k1 -> k2 and mutes only k2, then k1 — now the sole
    usable key — gets its free passes before it too mutes.
    """
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))
    assert adapter._key_pool is not None

    def always_429(*args, **kwargs):
        return _make_stream_gen(status=429, retry_after="1")

    with patch.object(adapter.http, "stream_post", side_effect=always_429):
        for _ in range(adapter._key_pool.SOLE_KEY_BACKOFF_THRESHOLD + 1):
            with pytest.raises(aiohttp.ClientResponseError) as exc_info:
                async for _ in adapter.stream_chat_completion([{"role": "user", "content": "hi"}]):
                    pass  # pragma: no cover — generator is expected to raise before yielding
            assert exc_info.value.status == 429

    assert adapter._key_pool._keys[0].cooldown_until > 0
    assert adapter._key_pool._keys[1].cooldown_until > 0


async def test_streaming_mid_stream_error_mutes_key():
    """An error after the first chunk mutes the key via the error-status release.

    Mid-stream there is nothing to rotate onto — the upstream has already begun
    answering — so this release carries no ``tried`` set and takes the mute path,
    moving the *next* request off the key.
    """
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


async def test_cancelled_non_affine_post_releases_recovery_probe():
    """Cancelling a pooled POST cannot leave its recovery probe claimed."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))
    assert adapter._key_pool is not None

    _, failed = adapter._key_pool.acquire(None)
    adapter._key_pool.release(failed, status_code=503, tried={0})
    adapter._key_pool._non_affine_reprobe_at[None] = 0

    started = asyncio.Event()
    release_upstream = asyncio.Event()

    async def blocked_json_post(*args, **kwargs):
        started.set()
        await release_upstream.wait()
        return {}

    with (
        req_ctx.push(affinity_key=None, user_role=None),
        patch.object(
            adapter, "_acquire_upstream_slot", new=AsyncMock(return_value=UpstreamSlot(None))
        ),
        patch.object(adapter.http, "json_post", side_effect=blocked_json_post),
    ):
        task = asyncio.create_task(adapter._post_with_pool("https://example.test", {}))
        await asyncio.wait_for(started.wait(), timeout=1)
        assert adapter._key_pool._non_affine_reprobe_in_flight.get(None) == 0
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert None not in adapter._key_pool._non_affine_reprobe_in_flight


async def test_cancelled_before_slot_acquisition_releases_lease_neutrally_once():
    """Cancellation while waiting for a slot cannot strand a pool lease."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))
    assert adapter._key_pool is not None

    _, failed = adapter._key_pool.acquire(None)
    adapter._key_pool.release(failed, status_code=503, tried={0})
    adapter._key_pool._non_affine_reprobe_at[None] = 0

    started = asyncio.Event()
    slot_available = asyncio.Event()

    async def blocked_slot(*args, **kwargs):
        started.set()
        await slot_available.wait()
        return UpstreamSlot(None)

    with (
        req_ctx.push(affinity_key=None, user_role=None),
        patch.object(adapter, "_acquire_upstream_slot", side_effect=blocked_slot),
        patch.object(adapter._key_pool, "release", wraps=adapter._key_pool.release) as release,
    ):
        task = asyncio.create_task(adapter._post_with_pool("https://example.test", {}))
        await asyncio.wait_for(started.wait(), timeout=1)
        assert adapter._key_pool._non_affine_reprobe_in_flight.get(None) == 0
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    release.assert_called_once()
    assert release.call_args.kwargs["status_code"] is None
    assert None not in adapter._key_pool._non_affine_reprobe_in_flight


async def test_unexpected_post_exception_releases_lease_neutrally_once():
    """An arbitrary POST exception cannot strand a non-affine recovery lease."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))
    assert adapter._key_pool is not None

    _, failed = adapter._key_pool.acquire(None)
    adapter._key_pool.release(failed, status_code=503, tried={0})
    adapter._key_pool._non_affine_reprobe_at[None] = 0

    async def fail_json_post(*args, **kwargs):
        raise RuntimeError("unexpected adapter failure")

    with (
        req_ctx.push(affinity_key=None, user_role=None),
        patch.object(
            adapter, "_acquire_upstream_slot", new=AsyncMock(return_value=UpstreamSlot(None))
        ),
        patch.object(adapter.http, "json_post", side_effect=fail_json_post),
        patch.object(adapter._key_pool, "release", wraps=adapter._key_pool.release) as release,
        pytest.raises(RuntimeError, match="unexpected adapter failure"),
    ):
        await adapter._post_with_pool("https://example.test", {})

    release.assert_called_once()
    assert release.call_args.kwargs["status_code"] is None
    assert None not in adapter._key_pool._non_affine_reprobe_in_flight


async def test_cancelled_non_affine_stream_releases_recovery_probe():
    """Cancelling before the first stream chunk releases the probe lease."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))
    assert adapter._key_pool is not None

    _, failed = adapter._key_pool.acquire(None)
    adapter._key_pool.release(failed, status_code=503, tried={0})
    adapter._key_pool._non_affine_reprobe_at[None] = 0

    started = asyncio.Event()
    release_upstream = asyncio.Event()

    def blocked_stream(*args, **kwargs):
        async def stream():
            started.set()
            await release_upstream.wait()
            yield "data: {}\n\n"

        return stream()

    async def consume_stream():
        async for _ in adapter._open_stream_with_pool("https://example.test", {}, timeout=None):
            pass

    with (
        req_ctx.push(affinity_key=None, user_role=None),
        patch.object(
            adapter, "_acquire_upstream_slot", new=AsyncMock(return_value=UpstreamSlot(None))
        ),
        patch.object(adapter.http, "stream_post", side_effect=blocked_stream),
    ):
        task = asyncio.create_task(consume_stream())
        await asyncio.wait_for(started.wait(), timeout=1)
        assert adapter._key_pool._non_affine_reprobe_in_flight.get(None) == 0
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert None not in adapter._key_pool._non_affine_reprobe_in_flight


async def test_unexpected_stream_open_exception_releases_lease_neutrally_once():
    """A stream-construction exception cannot strand a recovery lease."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))
    assert adapter._key_pool is not None

    _, failed = adapter._key_pool.acquire(None)
    adapter._key_pool.release(failed, status_code=503, tried={0})
    adapter._key_pool._non_affine_reprobe_at[None] = 0

    def fail_stream(*args, **kwargs):
        raise RuntimeError("unexpected stream setup failure")

    with (
        req_ctx.push(affinity_key=None, user_role=None),
        patch.object(
            adapter, "_acquire_upstream_slot", new=AsyncMock(return_value=UpstreamSlot(None))
        ),
        patch.object(adapter.http, "stream_post", side_effect=fail_stream),
        patch.object(adapter._key_pool, "release", wraps=adapter._key_pool.release) as release,
        pytest.raises(RuntimeError, match="unexpected stream setup failure"),
    ):
        async for _ in adapter.stream_chat_completion([{"role": "user", "content": "hi"}]):
            pass

    release.assert_called_once()
    assert release.call_args.kwargs["status_code"] is None
    assert None not in adapter._key_pool._non_affine_reprobe_in_flight


async def test_reserved_key_is_spent_only_by_an_entitled_caller():
    """A pro-reserved key serves pro traffic and stays invisible to free traffic.

    Exercises the whole adapter path: the caller's role reaches the pool from
    ``req_ctx``, which the API-key auth dependency populates in production.
    """
    from serving.utils import context as req_ctx

    adapter = OpenAICompatAdapter(_make_config(["shared", "reserved"]))
    assert adapter._key_pool is not None
    adapter._key_pool.set_key_min_role("reserved", "pro")

    success_payload = {
        "id": "x",
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        "usage": {},
    }
    used: list[str] = []

    async def capture(url, json, headers, timeout):
        used.append(headers["Authorization"].removeprefix("Bearer "))
        return success_payload

    with patch.object(adapter.http, "json_post", side_effect=capture):
        with req_ctx.push(user_role="free", auth_key_hash="free-user"):
            await adapter.chat_completion([{"role": "user", "content": "hi"}])
        with req_ctx.push(user_role="pro", auth_key_hash="pro-user"):
            await adapter.chat_completion([{"role": "user", "content": "hi"}])
        # No role at all — an internal caller (probe/warmup) is unrestricted and
        # follows the same reserved-first preference as an entitled user.
        with req_ctx.push(auth_key_hash="probe"):
            await adapter.chat_completion([{"role": "user", "content": "hi"}])

    assert used == ["shared", "reserved", "reserved"]


async def test_free_caller_fails_over_when_only_reserved_keys_remain():
    """With every shared key muted, a free caller cannot borrow the reserved one.

    The adapter raises instead, which the router surfaces as an upstream failure
    and fails over to the next provider — rather than quietly spending premium
    capacity on a free-tier request.
    """
    from serving.adapters.key_pool import KeyPoolExhausted
    from serving.utils import context as req_ctx

    adapter = OpenAICompatAdapter(_make_config(["shared", "reserved"]))
    assert adapter._key_pool is not None
    adapter._key_pool.set_key_min_role("reserved", "pro")

    async def always_429(url, json, headers, timeout):
        raise _make_response_error(429, retry_after="1")

    # Burn both keys with a pro caller. Its first call takes the reserved key
    # (reserved-first), rotates onto the shared key when that 429s, and mutes
    # the shared key — the one it ran out of alternatives on. The reserved key
    # is then its sole key, so it costs one call per free pass before it mutes
    # too.
    with patch.object(adapter.http, "json_post", side_effect=always_429):
        for _ in range(adapter._key_pool.SOLE_KEY_BACKOFF_THRESHOLD + 1):
            with (
                pytest.raises(aiohttp.ClientResponseError),
                req_ctx.push(user_role="pro", auth_key_hash="pro-user"),
            ):
                await adapter.chat_completion([{"role": "user", "content": "hi"}])
    assert adapter._key_pool._keys[0].cooldown_until > 0  # shared
    assert adapter._key_pool._keys[1].cooldown_until > 0  # reserved

    # The free caller now has nothing usable at all.
    with (
        patch.object(adapter.http, "json_post", side_effect=always_429) as mock_post,
        pytest.raises(KeyPoolExhausted),
        req_ctx.push(user_role="free", auth_key_hash="free-user"),
    ):
        await adapter.chat_completion([{"role": "user", "content": "hi"}])
    assert mock_post.await_count == 0
