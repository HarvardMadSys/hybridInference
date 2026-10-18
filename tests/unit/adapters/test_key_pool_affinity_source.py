"""Where the key pool reads its per-caller affinity key from.

``KeyPool`` binds a caller to one upstream key so provider-side prompt caches
stay warm. Which caller that is comes from ``req_ctx``: the ``affinity_key``
every request surface publishes, with ``auth_key_hash`` as a fallback and the
``_anon`` sentinel only for internal traffic that has no caller identity at all.
An explicit ``None`` is non-sticky for unresolved anonymous requests.

The load-bearing property is *independence*: two distinct callers must get two
distinct bindings, so one caller's rotation can't drag another off its key.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import aiohttp
import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter, _pool_affinity_key
from serving.adapters.upstream_limiter import UpstreamConcurrencyLimiter
from serving.utils import context as req_ctx

_SUCCESS = {
    "id": "x",
    "choices": [{"message": {"role": "assistant", "content": "hi"}}],
    "usage": {},
}


def _make_adapter(api_keys: list[str]) -> OpenAICompatAdapter:
    return OpenAICompatAdapter(
        ModelConfig(
            id="test-model",
            name="test-model",
            provider="zai",
            base_url="https://api.example.com",
            api_keys=api_keys,
            provider_model_id="test-model",
        )
    )


async def _chat_as(adapter: OpenAICompatAdapter, ctx: dict) -> None:
    """Run one pooled completion with *ctx* as the whole request context."""
    req_ctx.set(ctx)

    async def fake_json_post(url, json, headers, timeout):
        return _SUCCESS

    with patch.object(adapter.http, "json_post", side_effect=fake_json_post):
        await adapter.chat_completion([{"role": "user", "content": "hi"}])


@pytest.mark.unit
def test_prefers_the_published_affinity_key():
    req_ctx.set({"affinity_key": "ip:203.0.113.7", "auth_key_hash": "_anon"})
    assert _pool_affinity_key() == "ip:203.0.113.7"


@pytest.mark.unit
def test_falls_back_to_auth_key_hash():
    """A producer that publishes only ``auth_key_hash`` still gets per-key stickiness."""
    req_ctx.set({"auth_key_hash": "deadbeef"})
    assert _pool_affinity_key() == "deadbeef"


@pytest.mark.unit
def test_anon_sentinel_only_without_any_caller_identity():
    """Health probes / warmups publish neither key and legitimately share a binding."""
    assert _pool_affinity_key() == "_anon"
    req_ctx.set({"affinity_key": None, "auth_key_hash": "auth-hash-must-not-fallback"})
    assert _pool_affinity_key() is None


async def test_two_callers_get_independent_affinity_entries():
    """Distinct callers bind separately, so neither inherits the other's key."""
    adapter = _make_adapter(["k1", "k2"])

    await _chat_as(adapter, {"affinity_key": "hash-a"})
    await _chat_as(adapter, {"affinity_key": "ip:198.51.100.4"})

    pool = adapter._key_pool
    assert pool is not None
    assert pool.affinity_count() == 2
    # Same key while both are healthy (selection is sequential) — independence is
    # about the *bindings*, which is what lets one caller rotate without the other.
    assert set(pool._affinity) == {"hash-a", "ip:198.51.100.4"}


async def test_unresolved_callers_do_not_create_a_shared_entry():
    """Explicitly unresolved callers bypass pool affinity entirely."""
    adapter = _make_adapter(["k1", "k2"])

    unresolved_context = {
        "affinity_key": None,
        "auth_key_hash": "auth-hash-must-not-fallback",
    }
    await _chat_as(adapter, unresolved_context)
    await _chat_as(adapter, unresolved_context)

    pool = adapter._key_pool
    assert pool is not None
    assert pool.affinity_count() == 0


@pytest.mark.unit
async def test_unresolved_identity_stays_nonsticky_across_key_rotation():
    """A credential hash cannot replace explicitly unresolved provenance."""
    adapter = _make_adapter(["k1", "k2"])
    used_keys: list[str] = []

    async def fake_json_post(url, json, headers, timeout):
        api_key = headers["Authorization"]
        used_keys.append(api_key)
        if api_key == "Bearer k1":
            raise aiohttp.ClientResponseError(
                request_info=None,
                history=(),
                status=429,
                message="rate limited",
            )
        return _SUCCESS

    context = {
        "affinity_key": None,
        "auth_key_hash": "auth-hash-must-not-fallback",
    }
    with patch.object(adapter.http, "json_post", side_effect=fake_json_post):
        for _ in range(2):
            req_ctx.set(context)
            await adapter.chat_completion([{"role": "user", "content": "hi"}])

    pool = adapter._key_pool
    assert pool is not None
    assert used_keys == ["Bearer k1", "Bearer k2", "Bearer k1", "Bearer k2"]
    assert pool.affinity_count() == 0
    assert "auth-hash-must-not-fallback" not in pool._affinity


@pytest.mark.unit
async def test_unresolved_identity_stays_nonsticky_after_stream_abandonment():
    """Closing an opened stream does not bind an unresolved caller to a key."""
    adapter = _make_adapter(["k1", "k2"])

    async def fake_stream_post(*_args, **_kwargs):
        yield 'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
        await asyncio.Event().wait()

    adapter.http.stream_post = fake_stream_post
    req_ctx.set(
        {
            "affinity_key": None,
            "auth_key_hash": "auth-hash-must-not-fallback",
        }
    )

    stream = adapter.stream_chat_completion([{"role": "user", "content": "hi"}])
    first_chunk = await asyncio.wait_for(stream.__anext__(), timeout=1)
    assert "first" in first_chunk
    assert adapter._key_pool is not None
    assert adapter._key_pool.affinity_count() == 0

    await stream.aclose()

    assert adapter._key_pool.affinity_count() == 0
    assert "auth-hash-must-not-fallback" not in adapter._key_pool._affinity


@pytest.mark.unit
async def test_unresolved_identity_stays_nonsticky_when_cancelled_while_queued():
    """Waiting for a real outbound slot does not bind auth fallback affinity."""
    adapter = _make_adapter(["k1", "k2"])
    limiter = UpstreamConcurrencyLimiter(
        initial_limit=1,
        max_limit=1,
        acquire_timeout=5.0,
    )
    provider = adapter._key_pool_provider_label
    base_url = adapter.config.base_url
    held = await limiter.acquire(provider, "k1", base_url=base_url)
    acquiring = asyncio.Event()

    async def acquire_slot(api_key: str | None):
        acquiring.set()
        return await limiter.acquire(provider, api_key, base_url=base_url)

    adapter._acquire_upstream_slot = acquire_slot
    context = {
        "affinity_key": None,
        "auth_key_hash": "auth-hash-must-not-fallback",
    }

    try:
        with req_ctx.push(**context):
            task = asyncio.create_task(
                adapter.chat_completion([{"role": "user", "content": "queued"}])
            )
            await asyncio.wait_for(acquiring.wait(), timeout=1)
            await asyncio.sleep(0)

            assert next(iter(limiter.snapshot().values()))["waiting"] == 1
            assert adapter._key_pool is not None
            assert adapter._key_pool.affinity_count() == 0

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert adapter._key_pool.affinity_count() == 0
            assert "auth-hash-must-not-fallback" not in adapter._key_pool._affinity
            assert next(iter(limiter.snapshot().values()))["waiting"] == 0
            assert req_ctx.get().get("affinity_key") is None
    finally:
        held.release(status_code=200)

    assert next(iter(limiter.snapshot().values()))["in_flight"] == 0
