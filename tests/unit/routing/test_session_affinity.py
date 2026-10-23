from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest

from routing.endpoints import endpoint_id_for_adapter
from routing.prefill_load import (
    AFFINITY_BACKLOG_CEILING,
    conversation_fingerprint,
    prompt_anchor,
)
from routing.protocols import RoutingRequestOptions
from routing.routers import (
    AFFINITY_MAX_AGE_SECONDS,
    AFFINITY_SWEEP_THRESHOLD,
    AFFINITY_TTL_SECONDS,
    FixedRouter,
    _Affinity,
)
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


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


class _FailAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        raise RuntimeError("fail")

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        raise RuntimeError("fail")
        yield  # pragma: no cover


@pytest.mark.unit
def test_constants_exposed():
    assert AFFINITY_TTL_SECONDS == 300.0
    assert AFFINITY_SWEEP_THRESHOLD == 1000
    assert AFFINITY_MAX_AGE_SECONDS == 86400.0


@pytest.mark.unit
def test_affinity_dict_initialized_empty():
    r = FixedRouter()
    assert r._affinity == {}


@pytest.mark.unit
def test_drop_affinity_no_op_when_no_key():
    r = FixedRouter()
    req_ctx.set({})
    r._drop_affinity("m")  # must not raise
    assert r._affinity == {}


@pytest.mark.unit
def test_drop_affinity_removes_entry():
    r = FixedRouter()
    r._affinity[("u1", "m")] = _Affinity(endpoint_id="p:host:1", expires_at=time.monotonic() + 60)
    req_ctx.set({"affinity_key": "u1"})
    r._drop_affinity("m")
    assert ("u1", "m") not in r._affinity


@pytest.mark.unit
def test_drop_affinity_other_models_untouched():
    r = FixedRouter()
    r._affinity[("u1", "m1")] = _Affinity(endpoint_id="p:host:1", expires_at=time.monotonic() + 60)
    r._affinity[("u1", "m2")] = _Affinity(endpoint_id="p:host:2", expires_at=time.monotonic() + 60)
    req_ctx.set({"affinity_key": "u1"})
    r._drop_affinity("m1")
    assert ("u1", "m1") not in r._affinity
    assert ("u1", "m2") in r._affinity


@pytest.mark.unit
def test_maybe_sweep_below_threshold_is_noop():
    r = FixedRouter()
    now = time.monotonic()
    r._affinity[("u1", "m")] = _Affinity(endpoint_id="p:host:1", expires_at=now - 1)
    with r._lock:
        r._maybe_sweep_affinity_locked(now)
    assert ("u1", "m") in r._affinity


@pytest.mark.unit
def test_maybe_sweep_above_threshold_drops_expired():
    r = FixedRouter()
    now = time.monotonic()
    for i in range(AFFINITY_SWEEP_THRESHOLD + 1):
        r._affinity[(f"u{i}", "m")] = _Affinity(
            endpoint_id="p:host:1",
            expires_at=now + (60 if i % 2 == 0 else -1),
        )
    with r._lock:
        r._maybe_sweep_affinity_locked(now)
    expired = [k for k, a in r._affinity.items() if a.expires_at < now]
    assert expired == []


@pytest.mark.unit
def test_first_pick_creates_entry():
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A", base_url="http://A"))
    b = _EchoAdapter(_cfg("m", provider="B", base_url="http://B"))
    r.register_route("m", [(a, 0.5), (b, 0.5)])

    req_ctx.set({"affinity_key": "u1"})
    chosen = r._select_adapter("m")
    assert chosen is not None
    entry = r._affinity[("u1", "m")]
    assert entry.endpoint_id in {a.config.provider, b.config.provider}
    assert entry.expires_at > time.monotonic()


@pytest.mark.unit
def test_warm_prefix_outranks_the_weighted_draw(monkeypatch):
    """A conversation's resident prefix breaks a tie the draw cannot see.

    Without a locality term the draw hands this conversation to the cold replica,
    which is the measured staging symptom: identical-prefix requests reached the
    cache-capable endpoint only about half the time. The hint is the tracker's
    own, so this exercises route selection, not the sticky pin table -- no
    affinity entry exists when the draw runs. Weights are equal: locality is
    choosing between comparable candidates, not overriding a preference.
    """
    r = FixedRouter()
    cold = _EchoAdapter(_cfg("m", provider="A", base_url="http://A"))
    warm = _EchoAdapter(_cfg("m", provider="B", base_url="http://B"))
    r.register_route("m", [(cold, 0.5), (warm, 0.5)])

    warm_id = endpoint_id_for_adapter(warm)
    tracker = r.prefill_load
    tracker.release(
        tracker.acquire(warm_id, 5_000, affinity_key="u1", fingerprint="conv-a"),
        prefill_confirmed=True,
    )

    # Both draws choose index 0: the cold candidate.
    monkeypatch.setattr("random.random", lambda: 0.0)
    req_ctx.set({"affinity_key": "u1"})

    assert r._select_adapter("m", prefill_tokens=10_000, fingerprint="conv-a") is warm
    # Selection records what it chose, so the next turn pins to the warm endpoint.
    assert r._affinity[("u1", "m")].endpoint_id == warm_id


@pytest.mark.unit
def test_a_heavy_weight_preference_is_not_overturned_by_locality(monkeypatch):
    """Weights are cost and operator preference, not only a load split.

    The measured saving is large and the conversation is genuinely warm on the
    10%-weighted replica, which is exactly the situation the earlier revision
    rewarded unconditionally. A 10x weight difference is a decision the operator
    made; one avoided prefill does not outrank it.
    """
    r = FixedRouter()
    preferred = _EchoAdapter(_cfg("m", provider="A", base_url="http://A"))
    warm = _EchoAdapter(_cfg("m", provider="B", base_url="http://B"))
    r.register_route("m", [(preferred, 0.9), (warm, 0.1)])

    tracker = r.prefill_load
    tracker.release(
        tracker.acquire(
            endpoint_id_for_adapter(warm), 5_000, affinity_key="u1", fingerprint="conv-a"
        ),
        prefill_confirmed=True,
    )

    monkeypatch.setattr("random.random", lambda: 0.0)
    req_ctx.set({"affinity_key": "u1"})

    assert r._select_adapter("m", prefill_tokens=10_000, fingerprint="conv-a") is preferred


@pytest.mark.unit
def test_an_unrelated_conversation_does_not_inherit_the_keys_locality(monkeypatch):
    """One API key, two conversations: the hint table cannot tell them apart.

    ``affinity_key`` is the API-key hash, so the hint left by conversation A is
    visible to conversation B under the loose caller-scoped discount. Acting on
    it would move B to a replica that holds nothing of B's prompt -- and then
    re-pin the whole key there. The fingerprint is what distinguishes them.
    """
    r = FixedRouter()
    cold = _EchoAdapter(_cfg("m", provider="A", base_url="http://A"))
    warm = _EchoAdapter(_cfg("m", provider="B", base_url="http://B"))
    r.register_route("m", [(cold, 0.5), (warm, 0.5)])

    warm_id = endpoint_id_for_adapter(warm)
    tracker = r.prefill_load
    tracker.release(
        tracker.acquire(warm_id, 5_000, affinity_key="u1", fingerprint="conv-a"),
        prefill_confirmed=True,
    )
    # The caller-scoped number is what a cache-blind selection would have used.
    assert tracker.uncached_estimate(warm_id, 10_000, "u1") == 5_000

    monkeypatch.setattr("random.random", lambda: 0.0)
    req_ctx.set({"affinity_key": "u1"})

    assert r._select_adapter("m", prefill_tokens=10_000, fingerprint="conv-b") is cold


@pytest.mark.unit
def test_selection_without_a_fingerprint_keeps_the_draw(monkeypatch):
    """No conversation identity means no locality, not the caller-scoped guess."""
    r = FixedRouter()
    cold = _EchoAdapter(_cfg("m", provider="A", base_url="http://A"))
    warm = _EchoAdapter(_cfg("m", provider="B", base_url="http://B"))
    r.register_route("m", [(cold, 0.5), (warm, 0.5)])

    tracker = r.prefill_load
    tracker.release(
        tracker.acquire(
            endpoint_id_for_adapter(warm), 5_000, affinity_key="u1", fingerprint="conv-a"
        ),
        prefill_confirmed=True,
    )

    monkeypatch.setattr("random.random", lambda: 0.0)
    req_ctx.set({"affinity_key": "u1"})

    assert r._select_adapter("m", prefill_tokens=10_000) is cold


@pytest.mark.unit
def test_backlogged_warm_endpoint_does_not_capture_the_request(monkeypatch):
    """Locality loses to capacity: a queue past the ceiling costs more than prefill."""
    r = FixedRouter()
    cold = _EchoAdapter(_cfg("m", provider="A", base_url="http://A"))
    warm = _EchoAdapter(_cfg("m", provider="B", base_url="http://B"))
    r.register_route("m", [(cold, 0.5), (warm, 0.5)])

    warm_id = endpoint_id_for_adapter(warm)
    tracker = r.prefill_load
    tracker.release(
        tracker.acquire(warm_id, 5_000, affinity_key="u1", fingerprint="conv-a"),
        prefill_confirmed=True,
    )
    # Someone else's cold mega-prefill is now queued on the warm endpoint.
    tracker.acquire(warm_id, AFFINITY_BACKLOG_CEILING + 1, affinity_key="other")

    monkeypatch.setattr("random.random", lambda: 0.0)
    req_ctx.set({"affinity_key": "u1"})

    assert r._select_adapter("m", prefill_tokens=10_000, fingerprint="conv-a") is cold


@pytest.mark.unit
def test_locality_does_not_override_an_explicit_pin():
    """An operator's pin collapses the route; locality must not second-guess it."""
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A", base_url="http://A"))
    b = _EchoAdapter(_cfg("m", provider="B", base_url="http://B"))
    r.register_route("m", [(a, 0.9), (b, 0.1)])

    a_id = endpoint_id_for_adapter(a)
    tracker = r.prefill_load
    tracker.release(
        tracker.acquire(a_id, 5_000, affinity_key="u1", fingerprint="conv-a"),
        prefill_confirmed=True,
    )
    req_ctx.set({"affinity_key": "u1"})

    # The draw would avoid 'A', and 'A' is the worst possible choice by weight,
    # but the pin is the caller's instruction.
    assert r._select_adapter("m", pin_provider=a_id, prefill_tokens=10_000) is a


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chat_completion_routes_a_warm_conversation_to_its_prefix(monkeypatch):
    """End to end: the request path must actually carry the fingerprint.

    The selection-level tests call ``_select_adapter`` directly, so they cannot
    catch a call site that forgets to pass the conversation identity. This one
    goes through ``chat_completion`` and checks where the dispatch landed.
    """
    r = FixedRouter()
    cold = _EchoAdapter(_cfg("m", provider="A", base_url="http://A"))
    warm = _EchoAdapter(_cfg("m", provider="B", base_url="http://B"))
    r.register_route("m", [(cold, 0.5), (warm, 0.5)])
    warm_id = endpoint_id_for_adapter(warm)

    messages = [{"role": "user", "content": "k" * 40_000}]
    tracker = r.prefill_load
    tracker.release(
        tracker.acquire(
            warm_id,
            5_000,
            affinity_key="u1",
            fingerprint=conversation_fingerprint(messages),
            anchor=prompt_anchor(messages),
        ),
        prefill_confirmed=True,
    )

    monkeypatch.setattr("random.random", lambda: 0.0)
    req_ctx.set({"affinity_key": "u1"})

    await r.chat_completion("m", messages)

    assert r._affinity[("u1", "m")].endpoint_id == warm_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chat_completion_does_not_route_an_unrelated_conversation_by_locality(
    monkeypatch,
):
    """Same key, different conversation, warm hint present: no locality, no re-pin."""
    r = FixedRouter()
    cold = _EchoAdapter(_cfg("m", provider="A", base_url="http://A"))
    warm = _EchoAdapter(_cfg("m", provider="B", base_url="http://B"))
    r.register_route("m", [(cold, 0.5), (warm, 0.5)])

    theirs = [{"role": "user", "content": "k" * 40_000}]
    ours = [{"role": "user", "content": "z" * 40_000}]
    tracker = r.prefill_load
    tracker.release(
        tracker.acquire(
            endpoint_id_for_adapter(warm),
            5_000,
            affinity_key="u1",
            fingerprint=conversation_fingerprint(theirs),
            anchor=prompt_anchor(theirs),
        ),
        prefill_confirmed=True,
    )

    monkeypatch.setattr("random.random", lambda: 0.0)
    req_ctx.set({"affinity_key": "u1"})

    await r.chat_completion("m", ours)

    assert r._affinity[("u1", "m")].endpoint_id == endpoint_id_for_adapter(cold)


@pytest.mark.unit
def test_repeat_pick_reuses_endpoint():
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A", base_url="http://A"))
    b = _EchoAdapter(_cfg("m", provider="B", base_url="http://B"))
    r.register_route("m", [(a, 0.5), (b, 0.5)])

    req_ctx.set({"affinity_key": "u1"})
    first = r._select_adapter("m")
    for _ in range(20):
        again = r._select_adapter("m")
        assert again is first


@pytest.mark.unit
def test_repeat_pick_slides_ttl():
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A"))
    r.register_route("m", [(a, 1.0)])
    req_ctx.set({"affinity_key": "u1"})

    r._select_adapter("m")
    first_expiry = r._affinity[("u1", "m")].expires_at
    time.sleep(0.01)
    r._select_adapter("m")
    second_expiry = r._affinity[("u1", "m")].expires_at
    assert second_expiry > first_expiry


@pytest.mark.unit
def test_expired_entry_repicks():
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A"))
    r.register_route("m", [(a, 1.0)])
    req_ctx.set({"affinity_key": "u1"})

    r._select_adapter("m")
    r._affinity[("u1", "m")].expires_at = time.monotonic() - 1
    chosen = r._select_adapter("m")
    assert chosen is a
    assert r._affinity[("u1", "m")].expires_at > time.monotonic()


@pytest.mark.unit
def test_busy_pin_is_redrawn_once_it_reaches_max_age():
    """A caller that never pauses is re-drawn by weight once its pin is a day old.

    The sliding TTL alone would keep it wherever a failover or backlog re-pick
    last moved it, whatever the weights say. The clock is monotonic, so the pin
    is aged rather than slept through: moving only ``created_at`` back stands in
    for a day of renewals, which would have kept ``expires_at`` current.
    """
    r = FixedRouter()
    heavy = _EchoAdapter(_cfg("m", provider="HEAVY", base_url="http://HEAVY"))
    light = _EchoAdapter(_cfg("m", provider="LIGHT", base_url="http://LIGHT"))
    r.register_route("m", [(heavy, 1.0), (light, 1e-9)])
    req_ctx.set({"affinity_key": "u1"})

    r._record_affinity_and_return("m", light, "u1")
    pin = r._affinity[("u1", "m")]
    pin.created_at -= AFFINITY_MAX_AGE_SECONDS - 10

    # Ten seconds of life left: honored, but the renewal stops at the cap.
    assert r._select_adapter("m") is light
    assert pin.expires_at == pin.created_at + AFFINITY_MAX_AGE_SECONDS

    # Eleven seconds later the cap has passed, however busy the caller stayed.
    pin.created_at -= 11
    pin.expires_at -= 11
    assert r._select_adapter("m") is heavy
    fresh = r._affinity[("u1", "m")]
    assert fresh is not pin
    assert fresh.endpoint_id == endpoint_id_for_adapter(heavy)
    assert fresh.expires_at > time.monotonic()


@pytest.mark.unit
def test_max_age_zero_turns_the_cap_off(monkeypatch):
    import routing.routers as rr

    monkeypatch.setattr(rr, "AFFINITY_MAX_AGE_SECONDS", 0.0)
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A"))
    r.register_route("m", [(a, 1.0)])
    req_ctx.set({"affinity_key": "u1"})

    r._record_affinity_and_return("m", a, "u1")
    pin = r._affinity[("u1", "m")]
    pin.created_at -= 10 * 86400  # kept busy for ten days
    before = time.monotonic()
    assert r._select_adapter("m") is a
    assert pin.expires_at >= before + AFFINITY_TTL_SECONDS


@pytest.mark.unit
def test_pinned_endpoint_disabled_drops_entry():
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A", base_url="http://A"))
    b = _EchoAdapter(_cfg("m", provider="B", base_url="http://B"))
    r.register_route("m", [(a, 0.5), (b, 0.5)])

    req_ctx.set({"affinity_key": "u1"})
    r._affinity[("u1", "m")] = _Affinity(
        endpoint_id="GHOST",
        expires_at=time.monotonic() + 60,
    )
    chosen = r._select_adapter("m")
    assert chosen in {a, b}
    assert r._affinity[("u1", "m")].endpoint_id != "GHOST"


@pytest.mark.unit
def test_distinct_users_independent_entries():
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A"))
    b = _EchoAdapter(_cfg("m", provider="B"))
    r.register_route("m", [(a, 0.5), (b, 0.5)])

    req_ctx.set({"affinity_key": "u1"})
    r._select_adapter("m")
    req_ctx.set({"affinity_key": "u2"})
    r._select_adapter("m")
    assert ("u1", "m") in r._affinity
    assert ("u2", "m") in r._affinity


@pytest.mark.unit
def test_distinct_models_independent_entries():
    r = FixedRouter()
    a1 = _EchoAdapter(_cfg("m1", provider="A"))
    a2 = _EchoAdapter(_cfg("m2", provider="A"))
    r.register_route("m1", [(a1, 1.0)])
    r.register_route("m2", [(a2, 1.0)])

    req_ctx.set({"affinity_key": "u1"})
    r._select_adapter("m1")
    r._select_adapter("m2")
    assert ("u1", "m1") in r._affinity
    assert ("u1", "m2") in r._affinity


@pytest.mark.unit
def test_pin_provider_overrides_affinity():
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A"))
    b = _EchoAdapter(_cfg("m", provider="B"))
    r.register_route("m", [(a, 0.5), (b, 0.5)])

    req_ctx.set({"affinity_key": "u1"})
    r._affinity[("u1", "m")] = _Affinity(
        endpoint_id=a.config.provider,
        expires_at=time.monotonic() + 60,
    )
    chosen = r._select_adapter("m", pin_provider="B")
    assert chosen is b


@pytest.mark.unit
def test_no_affinity_key_no_entry():
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A"))
    r.register_route("m", [(a, 1.0)])

    req_ctx.set({})
    r._select_adapter("m")
    assert r._affinity == {}


@pytest.mark.unit
def test_disabled_via_env(monkeypatch):
    """When AFFINITY_ENABLED is False, no entries are written or read."""
    import routing.routers as rr

    monkeypatch.setattr(rr, "AFFINITY_ENABLED", False)
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A"))
    r.register_route("m", [(a, 1.0)])
    req_ctx.set({"affinity_key": "u1"})
    r._select_adapter("m")
    assert r._affinity == {}


@pytest.mark.unit
def test_chat_completion_drops_affinity_on_primary_error():
    """Affinity entry is gone before fallback runs (regardless of fallback success)."""
    import asyncio

    r = FixedRouter()
    bad = _FailAdapter(_cfg("m", provider="BAD", base_url="http://BAD"))
    good = _EchoAdapter(_cfg("m", provider="GOOD", base_url="http://GOOD"))
    r.register_route("m", [(bad, 0.99), (good, 0.01)])

    req_ctx.set({"affinity_key": "u1"})
    # Pin to BAD so _select_adapter returns it deterministically via affinity.
    r._affinity[("u1", "m")] = _Affinity(
        endpoint_id=endpoint_id_for_adapter(bad),
        expires_at=time.monotonic() + 60,
    )

    # Fallback (good) succeeds; entry must have been dropped before fallback ran.
    # If it had not been dropped, the post-success path would never write a new
    # entry (writes happen in _select_adapter, not in the fallback branch),
    # so we'd see the stale BAD-pinned entry survive.
    resp = asyncio.run(r.chat_completion("m", []))
    assert resp is not None
    assert ("u1", "m") not in r._affinity


@pytest.mark.unit
def test_chat_completion_drops_affinity_when_all_fail():
    """Affinity dropped even when no fallback is available."""
    import asyncio

    r = FixedRouter()
    bad = _FailAdapter(_cfg("m", provider="BAD"))
    r.register_route("m", [(bad, 1.0)])

    req_ctx.set({"affinity_key": "u1"})
    r._affinity[("u1", "m")] = _Affinity(
        endpoint_id=endpoint_id_for_adapter(bad),
        expires_at=time.monotonic() + 60,
    )

    with pytest.raises(RuntimeError):
        asyncio.run(r.chat_completion("m", []))
    assert ("u1", "m") not in r._affinity


@pytest.mark.unit
def test_stream_chat_completion_drops_affinity_on_primary_error():
    import asyncio

    r = FixedRouter()
    bad = _FailAdapter(_cfg("m", provider="BAD"))
    r.register_route("m", [(bad, 1.0)])

    req_ctx.set({"affinity_key": "u1"})
    r._affinity[("u1", "m")] = _Affinity(
        endpoint_id=endpoint_id_for_adapter(bad),
        expires_at=time.monotonic() + 60,
    )

    async def _consume():
        async for _ in r.stream_chat_completion("m", []):
            pass

    with pytest.raises(RuntimeError):
        asyncio.run(_consume())
    assert ("u1", "m") not in r._affinity


@pytest.mark.unit
def test_concurrent_acquires_yield_one_entry():
    import threading

    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A"))
    b = _EchoAdapter(_cfg("m", provider="B"))
    r.register_route("m", [(a, 0.5), (b, 0.5)])

    chosen: list[BaseAdapter] = []
    barrier = threading.Barrier(20)

    def _worker() -> None:
        req_ctx.set({"affinity_key": "u_race"})
        barrier.wait()
        sel = r._select_adapter("m")
        if sel is not None:
            chosen.append(sel)

    threads = [threading.Thread(target=_worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one entry survives; all picks are valid adapters.
    assert ("u_race", "m") in r._affinity
    assert all(c in {a, b} for c in chosen)


@pytest.mark.unit
def test_pin_provider_failure_preserves_affinity_chat():
    """A failed pin_provider request must not drop the user's existing affinity."""
    import asyncio

    r = FixedRouter()
    bad = _FailAdapter(_cfg("m", provider="BAD", base_url="http://BAD"))
    good = _EchoAdapter(_cfg("m", provider="GOOD", base_url="http://GOOD"))
    r.register_route("m", [(bad, 0.5), (good, 0.5)])

    req_ctx.set({"affinity_key": "u1"})
    r._affinity[("u1", "m")] = _Affinity(
        endpoint_id=endpoint_id_for_adapter(good),
        expires_at=time.monotonic() + 60,
    )

    with pytest.raises(RuntimeError):
        asyncio.run(
            r.chat_completion(
                "m",
                [],
                routing_options=RoutingRequestOptions(pin_provider="BAD"),
            )
        )

    assert ("u1", "m") in r._affinity
    assert r._affinity[("u1", "m")].endpoint_id == endpoint_id_for_adapter(good)


@pytest.mark.unit
def test_pin_provider_failure_preserves_affinity_stream():
    """A failed pin_provider stream must not drop the user's existing affinity."""
    import asyncio

    r = FixedRouter()
    bad = _FailAdapter(_cfg("m", provider="BAD", base_url="http://BAD"))
    good = _EchoAdapter(_cfg("m", provider="GOOD", base_url="http://GOOD"))
    r.register_route("m", [(bad, 0.5), (good, 0.5)])

    req_ctx.set({"affinity_key": "u1"})
    r._affinity[("u1", "m")] = _Affinity(
        endpoint_id=endpoint_id_for_adapter(good),
        expires_at=time.monotonic() + 60,
    )

    async def _consume():
        async for _ in r.stream_chat_completion(
            "m",
            [],
            routing_options=RoutingRequestOptions(pin_provider="BAD"),
        ):
            pass

    with pytest.raises(RuntimeError):
        asyncio.run(_consume())

    assert ("u1", "m") in r._affinity
    assert r._affinity[("u1", "m")].endpoint_id == endpoint_id_for_adapter(good)
