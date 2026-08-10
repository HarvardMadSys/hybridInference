"""Where the key pool reads its per-caller affinity key from.

``KeyPool`` binds a caller to one upstream key so provider-side prompt caches
stay warm. Which caller that is comes from ``req_ctx``: the ``affinity_key``
every request surface publishes, with ``auth_key_hash`` as a fallback and the
``_anon`` sentinel only for internal traffic that has no caller identity at all.

The load-bearing property is *independence*: two distinct callers must get two
distinct bindings, so one caller's rotation can't drag another off its key.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter, _pool_affinity_key
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


@pytest.fixture(autouse=True)
def _clean_context():
    req_ctx.set({})
    yield
    req_ctx.set({})


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
    req_ctx.set({"affinity_key": None, "auth_key_hash": "_anon"})
    assert _pool_affinity_key() == "_anon"


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


async def test_callers_without_identity_share_one_entry():
    """The contrast case: no published identity still collapses onto ``_anon``."""
    adapter = _make_adapter(["k1", "k2"])

    await _chat_as(adapter, {})
    await _chat_as(adapter, {})

    pool = adapter._key_pool
    assert pool is not None
    assert set(pool._affinity) == {"_anon"}
