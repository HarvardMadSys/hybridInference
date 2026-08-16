"""Unit tests for in-flight prefill accounting and prefill-aware selection."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest

from routing import prefill_load
from routing.prefill_load import (
    PrefillLoadTracker,
    estimate_prefill_tokens,
)
from routing.routers import FixedRouter, _Affinity
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture(autouse=True)
def _reset_req_ctx():
    """Reset req_ctx between tests so affinity_key does not leak."""
    req_ctx.set({})
    yield
    req_ctx.set({})


def _cfg(mid: str, provider: str = "p", base_url: str = "http://test") -> ModelConfig:
    return ModelConfig(
        id=mid,
        name=mid,
        provider=provider,
        base_url=base_url,
        context_length=8192,
        max_output_length=4096,
    )


class _EchoAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        yield self.format_stream_chunk(model=self.config.id, content="ok")


class _KeepAliveAdapter(BaseAdapter):
    """Emits an empty delta before any real token, so prefill is still in flight."""

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        yield self.format_stream_chunk(model=self.config.id, content="")
        yield self.format_stream_chunk(model=self.config.id, content="ok")


class _FailAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        raise RuntimeError("fail")

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        raise RuntimeError("fail")
        yield  # pragma: no cover


def _fixed_rand(*values: float):
    """Return a rand() callable yielding ``values`` in order, then repeating the last."""
    seq = list(values)

    def _rand() -> float:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return _rand


# ---------------------------------------------------------------------------
# estimate_prefill_tokens
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_estimate_empty_and_none():
    assert estimate_prefill_tokens(None) == 0
    assert estimate_prefill_tokens([]) == 0


@pytest.mark.unit
def test_estimate_scales_with_text_length():
    small = estimate_prefill_tokens([{"role": "user", "content": "x" * 400}])
    large = estimate_prefill_tokens([{"role": "user", "content": "x" * 4000}])
    assert small == 100
    assert large == 1000


@pytest.mark.unit
def test_estimate_sums_across_messages():
    messages = [
        {"role": "system", "content": "y" * 40},
        {"role": "user", "content": "z" * 40},
    ]
    assert estimate_prefill_tokens(messages) == 20


@pytest.mark.unit
def test_estimate_multimodal_blocks_do_not_char_count_base64():
    """A base64 image must contribute a flat cost, not its payload length."""
    huge_b64 = "A" * 500_000
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{huge_b64}"}},
            ],
        }
    ]
    # Flat image cost (85 tokens) plus the 2-char text, never the 500k payload.
    assert estimate_prefill_tokens(messages) < 200


@pytest.mark.unit
def test_estimate_tolerates_malformed_messages():
    assert estimate_prefill_tokens([None, 5, {"role": "user"}]) == 0  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# Tracker accounting
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_acquire_and_release_roundtrip():
    t = PrefillLoadTracker()
    assert t.backlog("e1") == 0
    lease = t.acquire("e1", 1000)
    assert t.backlog("e1") == 1000
    t.release(lease)
    assert t.backlog("e1") == 0


@pytest.mark.unit
def test_release_is_idempotent():
    """The stream path releases on first token and again in finally."""
    t = PrefillLoadTracker()
    lease = t.acquire("e1", 500)
    t.release(lease)
    t.release(lease)
    t.release(lease)
    assert t.backlog("e1") == 0


@pytest.mark.unit
def test_release_none_is_noop():
    PrefillLoadTracker().release(None)


@pytest.mark.unit
def test_concurrent_leases_accumulate():
    t = PrefillLoadTracker()
    a = t.acquire("e1", 100)
    t.acquire("e1", 250)
    assert t.backlog("e1") == 350
    t.release(a)
    assert t.backlog("e1") == 250


@pytest.mark.unit
def test_elephant_counters():
    t = PrefillLoadTracker(elephant_tokens=1000, elephant_limit=1)
    assert not t.is_elephant(999)
    assert t.is_elephant(1000)
    small = t.acquire("e1", 10)
    assert t.elephants("e1") == 0
    big = t.acquire("e1", 5000)
    assert t.elephants("e1") == 1
    t.release(big)
    assert t.elephants("e1") == 0
    t.release(small)


@pytest.mark.unit
def test_negative_tokens_clamped():
    t = PrefillLoadTracker()
    lease = t.acquire("e1", -50)
    assert t.backlog("e1") == 0
    t.release(lease)
    assert t.backlog("e1") == 0


@pytest.mark.unit
def test_snapshot_reports_load():
    t = PrefillLoadTracker(elephant_tokens=100)
    t.acquire("e1", 500)
    assert t.snapshot() == {"e1": {"prefill_tokens": 500, "elephants": 1}}


# ---------------------------------------------------------------------------
# select_index
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_select_single_candidate():
    t = PrefillLoadTracker()
    assert t.select_index(["a"], [1.0], 10, _fixed_rand(0.99)) == 0


@pytest.mark.unit
def test_select_rejects_empty():
    with pytest.raises(ValueError, match="at least one candidate"):
        PrefillLoadTracker().select_index([], [], 0, _fixed_rand(0.0))


@pytest.mark.unit
def test_select_prefers_lighter_endpoint():
    """Power-of-two-choices must break toward the endpoint holding less prefill."""
    t = PrefillLoadTracker()
    t.acquire("busy", 500_000)
    # Two draws land on index 0 (busy) then index 1 (idle); idle must win.
    chosen = t.select_index(["busy", "idle"], [0.5, 0.5], 100, _fixed_rand(0.1, 0.9))
    assert chosen == 1


@pytest.mark.unit
def test_select_keeps_choice_when_both_draws_match():
    t = PrefillLoadTracker()
    chosen = t.select_index(["a", "b"], [0.5, 0.5], 100, _fixed_rand(0.1, 0.1))
    assert chosen == 0


@pytest.mark.unit
def test_select_respects_weights_over_many_draws():
    """A 10x weight must still be drawn far more often when load is equal."""
    import random

    t = PrefillLoadTracker()
    rng = random.Random(1234)
    counts = [0, 0]
    for _ in range(2000):
        counts[t.select_index(["a", "b"], [0.9, 0.1], 100, rng.random)] += 1
    assert counts[0] > counts[1] * 3


@pytest.mark.unit
def test_elephant_avoids_saturated_endpoint():
    """A second elephant must not stack onto the endpoint already prefilling one."""
    t = PrefillLoadTracker(elephant_tokens=1000, elephant_limit=1)
    t.acquire("a", 5000)  # 'a' now holds one elephant
    # Even with a draw that would otherwise pick index 0, 'a' is excluded.
    chosen = t.select_index(["a", "b"], [0.5, 0.5], 5000, _fixed_rand(0.0))
    assert chosen == 1


@pytest.mark.unit
def test_elephant_falls_back_when_all_saturated():
    """Routing degrades to least-loaded, never to refusing to route."""
    t = PrefillLoadTracker(elephant_tokens=1000, elephant_limit=1)
    t.acquire("a", 900_000)
    t.acquire("b", 200_000)
    chosen = t.select_index(["a", "b"], [0.5, 0.5], 5000, _fixed_rand(0.0, 0.9))
    assert chosen == 1  # both saturated -> lighter one wins


@pytest.mark.unit
def test_no_intervention_below_backlog_floor():
    """Ordinary load must leave configured weights alone."""
    t = PrefillLoadTracker()
    t.acquire("a", prefill_load.INTERVENE_TOKENS - 1)
    # 'a' is busier, but not blocked, so the weighted draw stands.
    assert t.select_index(["a", "b"], [0.5, 0.5], 100, _fixed_rand(0.1, 0.9)) == 0


@pytest.mark.unit
def test_intervention_engages_at_backlog_floor():
    t = PrefillLoadTracker()
    t.acquire("a", prefill_load.INTERVENE_TOKENS)
    assert t.select_index(["a", "b"], [0.5, 0.5], 100, _fixed_rand(0.1, 0.9)) == 1


@pytest.mark.unit
def test_small_request_not_restricted_by_elephant_limit():
    t = PrefillLoadTracker(elephant_tokens=1000, elephant_limit=1)
    t.acquire("a", 5000)
    # A small request may still be drawn onto 'a' when it is the only draw.
    assert t.select_index(["a"], [1.0], 10, _fixed_rand(0.0)) == 0


@pytest.mark.unit
def test_zero_weights_do_not_divide_by_zero():
    t = PrefillLoadTracker()
    assert t.select_index(["a", "b"], [0.0, 0.0], 10, _fixed_rand(0.5)) == 1


@pytest.mark.unit
def test_kill_switch_restores_single_weighted_draw(monkeypatch):
    monkeypatch.setattr(prefill_load, "PREFILL_AWARE_ENABLED", False)
    t = PrefillLoadTracker()
    t.acquire("busy", 500_000)
    # With the switch off the busy endpoint is chosen anyway: pure weighted draw.
    assert t.select_index(["busy", "idle"], [0.5, 0.5], 100, _fixed_rand(0.1, 0.9)) == 0


# ---------------------------------------------------------------------------
# Affinity ceiling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_affinity_kept_when_endpoint_idle():
    t = PrefillLoadTracker()
    assert t.should_keep_affinity("e1", ceiling=1000) is True


@pytest.mark.unit
def test_affinity_dropped_when_endpoint_backlogged():
    t = PrefillLoadTracker()
    t.acquire("e1", 5000)
    assert t.should_keep_affinity("e1", ceiling=1000) is False


@pytest.mark.unit
def test_affinity_ceiling_ignored_when_disabled(monkeypatch):
    monkeypatch.setattr(prefill_load, "PREFILL_AWARE_ENABLED", False)
    t = PrefillLoadTracker()
    t.acquire("e1", 5000)
    assert t.should_keep_affinity("e1", ceiling=1000) is True


# ---------------------------------------------------------------------------
# Router integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_router_breaks_affinity_when_pinned_endpoint_backlogged():
    """The whale case: a pinned key must not queue behind a mega-prefill."""
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A", base_url="http://A"))
    b = _EchoAdapter(_cfg("m", provider="B", base_url="http://B"))
    r.register_route("m", [(a, 0.5), (b, 0.5)])
    req_ctx.set({"affinity_key": "whale"})

    r._affinity[("whale", "m")] = _Affinity(endpoint_id="A", expires_at=time.monotonic() + 300)
    # Idle pin is honored.
    assert r._select_adapter("m") is a

    # Once A is saturated the pin yields and selection re-runs onto B.
    r.prefill_load.acquire("A", 10_000_000)
    assert r._select_adapter("m") is b
    assert r._affinity[("whale", "m")].endpoint_id == "B"


@pytest.mark.unit
def test_router_keeps_affinity_when_pinned_endpoint_idle():
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A", base_url="http://A"))
    b = _EchoAdapter(_cfg("m", provider="B", base_url="http://B"))
    r.register_route("m", [(a, 0.5), (b, 0.5)])
    req_ctx.set({"affinity_key": "u1"})
    r._affinity[("u1", "m")] = _Affinity(endpoint_id="A", expires_at=time.monotonic() + 300)
    for _ in range(20):
        assert r._select_adapter("m") is a


@pytest.mark.unit
def test_router_drops_affinity_when_pinned_endpoint_gone():
    """Pre-existing contract: a pin to an endpoint no longer allowed is discarded."""
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A", base_url="http://A"))
    r.register_route("m", [(a, 1.0)])
    req_ctx.set({"affinity_key": "u1"})
    r._affinity[("u1", "m")] = _Affinity(endpoint_id="GONE", expires_at=time.monotonic() + 300)
    assert r._select_adapter("m") is a
    assert r._affinity[("u1", "m")].endpoint_id == "A"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_releases_lease_on_first_token():
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A", base_url="http://A"))
    r.register_route("m", [(a, 1.0)])
    messages = [{"role": "user", "content": "x" * 4000}]
    async for _chunk in r.stream_chat_completion("m", messages):
        pass
    assert r.prefill_load.backlog("A") == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_holds_lease_until_first_token():
    """Guards the disconnect test below from passing vacuously."""
    r = FixedRouter()
    a = _KeepAliveAdapter(_cfg("m", provider="A", base_url="http://A"))
    r.register_route("m", [(a, 1.0)])
    messages = [{"role": "user", "content": "x" * 4000}]

    gen = r.stream_chat_completion("m", messages)
    await gen.__anext__()  # routing chunk
    await gen.__anext__()  # keep-alive: prefill still in flight
    assert r.prefill_load.backlog("A") == 1000
    await gen.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_releases_lease_on_client_disconnect():
    """An abandoned generator must not strand the lease and mark the endpoint busy."""
    r = FixedRouter()
    a = _KeepAliveAdapter(_cfg("m", provider="A", base_url="http://A"))
    r.register_route("m", [(a, 1.0)])
    messages = [{"role": "user", "content": "x" * 4000}]

    gen = r.stream_chat_completion("m", messages)
    await gen.__anext__()  # routing chunk
    await gen.__anext__()  # keep-alive, lease held
    await gen.aclose()  # simulates the client hanging up mid-prefill

    assert r.prefill_load.backlog("A") == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_releases_lease_when_upstream_fails():
    r = FixedRouter()
    a = _FailAdapter(_cfg("m", provider="A", base_url="http://A"))
    r.register_route("m", [(a, 1.0)])
    messages = [{"role": "user", "content": "x" * 4000}]

    with pytest.raises(RuntimeError, match="fail"):
        async for _chunk in r.stream_chat_completion("m", messages):
            pass
    assert r.prefill_load.backlog("A") == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_streaming_releases_lease_on_success_and_failure():
    r = FixedRouter()
    ok = _EchoAdapter(_cfg("m", provider="OK", base_url="http://OK"))
    r.register_route("m", [(ok, 1.0)])
    messages = [{"role": "user", "content": "x" * 4000}]
    await r.chat_completion("m", messages)
    assert r.prefill_load.backlog("OK") == 0

    r2 = FixedRouter()
    bad = _FailAdapter(_cfg("m", provider="BAD", base_url="http://BAD"))
    r2.register_route("m", [(bad, 1.0)])
    with pytest.raises(RuntimeError, match="fail"):
        await r2.chat_completion("m", messages)
    assert r2.prefill_load.backlog("BAD") == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_fallback_releases_both_leases():
    r = FixedRouter()
    bad = _FailAdapter(_cfg("m", provider="BAD", base_url="http://BAD"))
    good = _EchoAdapter(_cfg("m", provider="GOOD", base_url="http://GOOD"))
    r.register_route("m", [(bad, 1.0), (good, 1.0)])
    req_ctx.set({})
    messages = [{"role": "user", "content": "x" * 4000}]

    async for _chunk in r.stream_chat_completion("m", messages):
        pass
    assert r.prefill_load.backlog("BAD") == 0
    assert r.prefill_load.backlog("GOOD") == 0
