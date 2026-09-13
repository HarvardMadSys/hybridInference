"""Unit tests for the adaptive outbound concurrency limiter.

Covers the two halves separately: the AIMD controller and its FIFO waiter queue
in isolation, then the adapter wiring — the part that is easy to get wrong,
since a streaming slot has to survive the whole generation and come back on
every unwind path.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.adapters.upstream_limiter import (
    UpstreamConcurrencyLimiter,
    UpstreamSaturated,
    get_upstream_limiter,
    key_fingerprint,
    reset_upstream_limiter,
    upstream_slot,
)
from serving.http import AsyncHTTPClient

PROVIDER = "zai"
KEY_A = "sk-aaa"
KEY_B = "sk-bbb"
REMOTE = "https://api.z.ai/v4"
LOCAL = "http://localhost:12003/v1"


def _limiter(**kwargs) -> UpstreamConcurrencyLimiter:
    """Build a limiter with small, explicit tunables (never reads the env)."""
    defaults = {
        "initial_limit": 2,
        "max_limit": 4,
        "probe_success_interval": 3,
        "acquire_timeout": 0.05,
    }
    return UpstreamConcurrencyLimiter(**{**defaults, **kwargs})


def _state(limiter: UpstreamConcurrencyLimiter, key: str = KEY_A) -> dict[str, int]:
    return limiter.snapshot()[(PROVIDER, key_fingerprint(key))]


async def _acquire(limiter: UpstreamConcurrencyLimiter, key: str = KEY_A):
    return await limiter.acquire(PROVIDER, key, base_url=REMOTE)


# ---------------------------------------------------------------- accounting


async def test_in_flight_is_counted_on_acquire_and_returned_on_release():
    limiter = _limiter()

    first = await _acquire(limiter)
    assert _state(limiter)["in_flight"] == 1
    second = await _acquire(limiter)
    assert _state(limiter)["in_flight"] == 2

    first.release(status_code=200)
    assert _state(limiter)["in_flight"] == 1
    second.release(status_code=200)
    assert _state(limiter)["in_flight"] == 0


async def test_release_is_idempotent():
    """A stream can unwind twice; the second release must not free a stranger's slot."""
    limiter = _limiter()
    held = await _acquire(limiter)
    other = await _acquire(limiter)

    held.release(status_code=200)
    held.release(status_code=200)
    held.release()

    assert _state(limiter)["in_flight"] == 1
    assert not held.held
    assert other.held


# ------------------------------------------------------------------- queueing


async def test_saturated_request_waits_and_proceeds_when_a_slot_frees():
    limiter = _limiter(initial_limit=1, acquire_timeout=5.0)
    held = await _acquire(limiter)

    waiting = asyncio.ensure_future(_acquire(limiter))
    await asyncio.sleep(0)
    assert not waiting.done()
    assert _state(limiter)["waiting"] == 1

    held.release(status_code=200)
    granted = await waiting
    assert granted.held
    # The slot moved across without ever dipping below the limit.
    assert _state(limiter)["in_flight"] == 1
    assert _state(limiter)["waiting"] == 0
    granted.release(status_code=200)


async def test_waiters_are_served_in_fifo_order():
    limiter = _limiter(initial_limit=1, acquire_timeout=5.0)
    held = await _acquire(limiter)

    order: list[int] = []

    async def contender(index: int) -> None:
        slot = await _acquire(limiter)
        order.append(index)
        slot.release(status_code=200)

    tasks = []
    for index in range(3):
        tasks.append(asyncio.ensure_future(contender(index)))
        # One tick each, so the queue order is the arrival order rather than
        # whatever order the loop happens to start the tasks in.
        await asyncio.sleep(0)

    assert _state(limiter)["waiting"] == 3
    held.release(status_code=200)
    await asyncio.gather(*tasks)

    assert order == [0, 1, 2]


async def test_a_new_request_never_jumps_a_queued_one():
    limiter = _limiter(initial_limit=1, acquire_timeout=5.0)
    held = await _acquire(limiter)

    queued = asyncio.ensure_future(_acquire(limiter))
    await asyncio.sleep(0)

    latecomer = asyncio.ensure_future(_acquire(limiter))
    await asyncio.sleep(0)

    held.release(status_code=200)
    first_served = await queued
    assert first_served.held
    assert not latecomer.done()

    first_served.release(status_code=200)
    (await latecomer).release(status_code=200)


# -------------------------------------------------------------------- timeout


async def test_acquire_timeout_raises_and_leaves_no_waiter_behind():
    limiter = _limiter(initial_limit=1, acquire_timeout=0.01)
    held = await _acquire(limiter)

    with pytest.raises(UpstreamSaturated, match="No outbound slot"):
        await _acquire(limiter)

    assert _state(limiter)["waiting"] == 0
    assert _state(limiter)["in_flight"] == 1

    # The bucket is still usable once the holder is done — a timed-out waiter
    # must not have consumed the slot it never received.
    held.release(status_code=200)
    assert _state(limiter)["in_flight"] == 0
    (await _acquire(limiter)).release(status_code=200)


async def test_saturation_error_carries_no_http_status():
    """It must not read as a provider 429 to endpoint health or the key pool."""
    limiter = _limiter(initial_limit=1, acquire_timeout=0.01)
    await _acquire(limiter)

    with pytest.raises(UpstreamSaturated) as excinfo:
        await _acquire(limiter)

    from routing.endpoint_health import _http_status_of

    assert _http_status_of(excinfo.value) is None


async def test_cancelled_waiter_is_removed_and_does_not_hold_a_slot():
    limiter = _limiter(initial_limit=1, acquire_timeout=5.0)
    held = await _acquire(limiter)

    waiting = asyncio.ensure_future(_acquire(limiter))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert _state(limiter)["waiting"] == 0
    held.release(status_code=200)
    assert _state(limiter)["in_flight"] == 0


# ----------------------------------------------------------------------- AIMD


async def test_429_decrements_the_limit():
    limiter = _limiter(initial_limit=3)
    slot = await _acquire(limiter)
    slot.release(status_code=429)
    assert _state(limiter)["limit"] == 2


async def test_limit_never_falls_below_one():
    limiter = _limiter(initial_limit=2)
    for _ in range(10):
        slot = await _acquire(limiter)
        slot.release(status_code=429)
    assert _state(limiter)["limit"] == 1


async def test_probe_raises_the_limit_every_interval_up_to_max():
    limiter = _limiter(initial_limit=3, max_limit=4, probe_success_interval=3)

    for _ in range(2):
        (await _acquire(limiter)).release(status_code=200)
    assert _state(limiter)["limit"] == 3

    (await _acquire(limiter)).release(status_code=200)
    assert _state(limiter)["limit"] == 4

    # Capped: another full interval cannot push past max_limit.
    for _ in range(3):
        (await _acquire(limiter)).release(status_code=200)
    assert _state(limiter)["limit"] == 4


async def test_429_right_after_a_probe_reverts_it():
    limiter = _limiter(initial_limit=2, max_limit=8, probe_success_interval=2)

    for _ in range(2):
        (await _acquire(limiter)).release(status_code=200)
    assert _state(limiter)["limit"] == 3

    (await _acquire(limiter)).release(status_code=429)
    assert _state(limiter)["limit"] == 2
    # And the probe counter restarts, so the next probe needs a full interval.
    assert _state(limiter)["successes_since_probe"] == 0


async def test_429_resets_the_probe_counter():
    limiter = _limiter(initial_limit=4, probe_success_interval=3)

    (await _acquire(limiter)).release(status_code=200)
    (await _acquire(limiter)).release(status_code=200)
    (await _acquire(limiter)).release(status_code=429)
    assert _state(limiter)["successes_since_probe"] == 0
    limit_after_429 = _state(limiter)["limit"]

    # Two more 200s would have tripped a probe on the old counter.
    (await _acquire(limiter)).release(status_code=200)
    (await _acquire(limiter)).release(status_code=200)
    assert _state(limiter)["limit"] == limit_after_429


@pytest.mark.parametrize(
    "status_code",
    [
        400,  # request-scoped client error
        404,
        500,  # provider fault
        503,
        0,  # timeout / connection error sentinel
        204,  # 2xx that is not 200
        None,  # neutral release, outcome unknown
    ],
)
async def test_only_a_200_advances_the_probe_counter(status_code: int | None):
    """An error is not evidence of headroom, so it must never earn a probe."""
    limiter = _limiter(initial_limit=2, probe_success_interval=2)

    (await _acquire(limiter)).release(status_code=200)
    assert _state(limiter)["successes_since_probe"] == 1

    # Whatever this outcome is, it neither advances the counter nor resets it:
    # only a 429 resets, and only a 200 advances.
    (await _acquire(limiter)).release(status_code=status_code)
    assert _state(limiter)["successes_since_probe"] == 1
    assert _state(limiter)["limit"] == 2

    # ...and the next 200 is still the one that completes the interval.
    (await _acquire(limiter)).release(status_code=200)
    assert _state(limiter)["limit"] == 3


async def test_exactly_one_hundred_successes_trigger_a_probe_at_the_default():
    """99 is not enough; the 100th 200 is the one that raises the limit."""
    limiter = UpstreamConcurrencyLimiter(initial_limit=8, max_limit=64, acquire_timeout=1.0)

    for _ in range(99):
        (await _acquire(limiter)).release(status_code=200)
    assert _state(limiter)["limit"] == 8
    assert _state(limiter)["successes_since_probe"] == 99

    (await _acquire(limiter)).release(status_code=200)
    assert _state(limiter)["limit"] == 9
    assert _state(limiter)["successes_since_probe"] == 0


async def test_errors_between_successes_do_not_shorten_the_interval():
    limiter = UpstreamConcurrencyLimiter(initial_limit=8, max_limit=64, acquire_timeout=1.0)

    for _ in range(99):
        (await _acquire(limiter)).release(status_code=200)
    for status in (500, 0, 400, 503, 502):
        (await _acquire(limiter)).release(status_code=status)

    assert _state(limiter)["limit"] == 8  # five failures bought nothing
    (await _acquire(limiter)).release(status_code=200)
    assert _state(limiter)["limit"] == 9


async def test_neutral_release_moves_nothing():
    limiter = _limiter(initial_limit=2, probe_success_interval=2)
    (await _acquire(limiter)).release(status_code=None)
    (await _acquire(limiter)).release()
    assert _state(limiter)["limit"] == 2
    assert _state(limiter)["successes_since_probe"] == 0


async def test_a_probe_wakes_a_waiter_that_now_fits():
    limiter = _limiter(initial_limit=1, max_limit=4, probe_success_interval=1, acquire_timeout=5.0)
    held = await _acquire(limiter)

    waiting = asyncio.ensure_future(_acquire(limiter))
    await asyncio.sleep(0)
    assert not waiting.done()

    # One success trips the probe, taking the limit from 1 to 2 — which is room
    # for the queued request even though the holder released at the same moment
    # and a second request has already arrived.
    held.release(status_code=200)
    granted = await waiting
    assert _state(limiter)["limit"] == 2
    granted.release(status_code=200)


# ------------------------------------------------------------- bucket scoping


async def test_two_keys_of_one_provider_adapt_independently():
    limiter = _limiter(initial_limit=3)

    a = await _acquire(limiter, KEY_A)
    (await _acquire(limiter, KEY_B)).release(status_code=200)
    a.release(status_code=429)

    assert _state(limiter, KEY_A)["limit"] == 2
    assert _state(limiter, KEY_B)["limit"] == 3

    # And in-flight counts do not bleed across either: key A saturated must
    # leave key B free.
    held = [await _acquire(limiter, KEY_A) for _ in range(2)]
    assert _state(limiter, KEY_A)["in_flight"] == 2
    b = await _acquire(limiter, KEY_B)
    assert _state(limiter, KEY_B)["in_flight"] == 1
    for slot in (*held, b):
        slot.release(status_code=200)


async def test_the_same_key_under_two_providers_is_two_buckets():
    limiter = _limiter(initial_limit=3)
    (await limiter.acquire("zai", KEY_A, base_url=REMOTE)).release(status_code=429)
    await limiter.acquire("chutes", KEY_A, base_url=REMOTE)

    snapshot = limiter.snapshot()
    assert snapshot[("zai", key_fingerprint(KEY_A))]["limit"] == 2
    assert snapshot[("chutes", key_fingerprint(KEY_A))]["limit"] == 3


def test_fingerprint_never_exposes_the_key():
    fingerprint = key_fingerprint(KEY_A)
    assert len(fingerprint) == 12
    assert KEY_A not in fingerprint
    assert fingerprint != key_fingerprint(KEY_B)
    # Missing and blank credentials share one bucket rather than escaping.
    assert key_fingerprint(None) == key_fingerprint("") == key_fingerprint("   ")


# ------------------------------------------------------------------- bypasses


async def test_local_endpoints_are_never_limited():
    limiter = _limiter(initial_limit=1)
    slots = [await limiter.acquire(PROVIDER, KEY_A, base_url=LOCAL) for _ in range(5)]

    assert limiter.snapshot() == {}
    assert all(not slot.held for slot in slots)
    for slot in slots:
        slot.release(status_code=429)
    assert limiter.snapshot() == {}


@pytest.mark.parametrize(
    "base_url",
    ["http://127.0.0.1:8000/v1", "http://host.docker.internal:8004/v1", "http://0.0.0.0:8001"],
)
async def test_every_local_host_form_is_exempt(base_url: str):
    limiter = _limiter(initial_limit=1)
    for _ in range(3):
        await limiter.acquire(PROVIDER, KEY_A, base_url=base_url)
    assert limiter.snapshot() == {}


async def test_disabled_limiter_admits_everything():
    limiter = _limiter(enabled=False, initial_limit=1)
    slots = [await _acquire(limiter) for _ in range(10)]

    assert limiter.snapshot() == {}
    assert all(not slot.held for slot in slots)


# ------------------------------------------------------------ loop rebinding


def test_a_bucket_outliving_its_event_loop_resets_its_waiter_state():
    """Futures are loop-bound; a stale count would shrink the bucket forever."""
    limiter = _limiter(initial_limit=2)

    async def take_and_abandon() -> None:
        await _acquire(limiter)  # deliberately never released

    asyncio.run(take_and_abandon())
    assert _state(limiter)["in_flight"] == 1

    async def use_fresh_loop() -> int:
        slot = await _acquire(limiter)
        count = _state(limiter)["in_flight"]
        slot.release(status_code=200)
        return count

    # The orphaned slot is gone, but the learned limit survives.
    assert asyncio.run(use_fresh_loop()) == 1
    assert _state(limiter)["limit"] == 2


# --------------------------------------------------------------- singleton


async def test_module_singleton_is_cached_and_resettable():
    reset_upstream_limiter()
    try:
        first = get_upstream_limiter()
        assert get_upstream_limiter() is first

        installed = _limiter()
        reset_upstream_limiter(installed)
        assert get_upstream_limiter() is installed
    finally:
        reset_upstream_limiter()


# ------------------------------------------------------------ adapter wiring


def _adapter(**overrides) -> OpenAICompatAdapter:
    config = ModelConfig(
        id="glm-4.7",
        name="GLM 4.7",
        provider=PROVIDER,
        base_url=REMOTE,
        api_key=KEY_A,
        provider_model_id="glm-4.7",
        processor="default",
        supported_params=["temperature", "max_tokens"],
        **overrides,
    )
    adapter = OpenAICompatAdapter(config)
    # Give the adapter its own HTTP client rather than the process-wide
    # ``AsyncHTTPClient.shared()`` singleton. These tests stub methods on it, and
    # ``monkeypatch.setattr`` on an instance whose attribute really lives on the
    # class restores by writing the old *bound method* back as a permanent
    # instance attribute -- which then shadows any later class-level patch for
    # the rest of the worker process, silently un-stubbing other test files'
    # HTTP mocks. A throwaway client dies with the test instead.
    adapter.http = AsyncHTTPClient()
    return adapter


def _chunk(delta: dict, finish_reason: str | None = None) -> str:
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "glm-4.7",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"


@pytest.fixture
def installed_limiter():
    """Install a small limiter as the process-wide one for adapter tests."""
    limiter = _limiter(initial_limit=1, acquire_timeout=0.01)
    reset_upstream_limiter(limiter)
    yield limiter
    reset_upstream_limiter()


async def test_stream_holds_the_slot_for_the_whole_generation(installed_limiter, monkeypatch):
    """The slot must span the body, not just the response open."""
    adapter = _adapter()
    in_flight_during_stream: list[int] = []

    async def fake_stream_post(*_args, **_kwargs):
        yield _chunk({"role": "assistant"})
        in_flight_during_stream.append(_state(installed_limiter)["in_flight"])
        yield _chunk({"content": "hi"})
        in_flight_during_stream.append(_state(installed_limiter)["in_flight"])
        yield _chunk({}, finish_reason="stop")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(adapter.http, "stream_post", fake_stream_post)

    chunks = [c async for c in adapter.stream_chat_completion([{"role": "user", "content": "x"}])]

    assert chunks[-1].strip() == "data: [DONE]"
    # Held for every chunk of the generation...
    assert in_flight_during_stream == [1, 1]
    # ...and handed back once it ended, counted as one success toward a probe.
    assert _state(installed_limiter)["in_flight"] == 0
    assert _state(installed_limiter)["successes_since_probe"] == 1


async def test_stream_releases_the_slot_on_a_mid_stream_error(installed_limiter, monkeypatch):
    adapter = _adapter()

    async def fake_stream_post(*_args, **_kwargs):
        yield _chunk({"role": "assistant"})
        yield _chunk({"content": "partial"})
        raise aiohttp.ClientPayloadError("connection reset mid-stream")

    monkeypatch.setattr(adapter.http, "stream_post", fake_stream_post)

    with pytest.raises(aiohttp.ClientError):
        async for _ in adapter.stream_chat_completion([{"role": "user", "content": "x"}]):
            pass

    assert _state(installed_limiter)["in_flight"] == 0
    # A mid-stream I/O failure is not a 429, so the limit is untouched.
    assert _state(installed_limiter)["limit"] == 1


async def test_stream_releases_the_slot_when_the_client_disconnects(installed_limiter, monkeypatch):
    """Abandoning the generator mid-stream must still hand the slot back."""
    adapter = _adapter()

    async def fake_stream_post(*_args, **_kwargs):
        yield _chunk({"role": "assistant"})
        for _ in range(100):
            yield _chunk({"content": "."})

    monkeypatch.setattr(adapter.http, "stream_post", fake_stream_post)

    stream = adapter.stream_chat_completion([{"role": "user", "content": "x"}])
    await stream.__anext__()
    assert _state(installed_limiter)["in_flight"] == 1

    await stream.aclose()
    assert _state(installed_limiter)["in_flight"] == 0


async def test_stream_opening_429_feeds_the_limiter(installed_limiter, monkeypatch):
    adapter = _adapter()

    async def fake_stream_post(*_args, **_kwargs):
        raise aiohttp.ClientResponseError(
            request_info=None, history=(), status=429, message="rate limited"
        )
        yield  # pragma: no cover - generator marker

    monkeypatch.setattr(adapter.http, "stream_post", fake_stream_post)

    with pytest.raises(aiohttp.ClientResponseError):
        async for _ in adapter.stream_chat_completion([{"role": "user", "content": "x"}]):
            pass

    assert _state(installed_limiter)["in_flight"] == 0
    assert _state(installed_limiter)["limit"] == 1  # already at the floor


async def test_non_streaming_post_releases_on_success_and_on_429(installed_limiter, monkeypatch):
    adapter = _adapter()
    limiter_state: list[int] = []

    async def ok(**_kwargs):
        limiter_state.append(_state(installed_limiter)["in_flight"])
        return {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(adapter.http, "json_post_with_retry", ok)
    await adapter.chat_completion([{"role": "user", "content": "x"}])
    assert limiter_state == [1]
    assert _state(installed_limiter)["in_flight"] == 0

    async def rate_limited(**_kwargs):
        raise aiohttp.ClientResponseError(
            request_info=None, history=(), status=429, message="rate limited"
        )

    monkeypatch.setattr(adapter.http, "json_post_with_retry", rate_limited)
    with pytest.raises(aiohttp.ClientResponseError):
        await adapter.chat_completion([{"role": "user", "content": "x"}])
    assert _state(installed_limiter)["in_flight"] == 0


async def test_saturated_pool_rotates_to_a_sibling_key_before_failing(monkeypatch):
    """A full bucket on one key must try the next key, not fail the request."""
    limiter = _limiter(initial_limit=1, acquire_timeout=0.01)
    reset_upstream_limiter(limiter)
    try:
        adapter = _adapter(api_keys=[KEY_A, KEY_B])
        # Fill key A's only slot with a request that never finishes.
        await limiter.acquire(PROVIDER, KEY_A, base_url=REMOTE)

        used: list[str] = []

        async def capture(url, json, headers, timeout):
            used.append(headers["Authorization"])
            return {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}

        monkeypatch.setattr(adapter.http, "json_post", capture)
        await adapter.chat_completion([{"role": "user", "content": "x"}])

        assert used == [f"Bearer {KEY_B}"]
        # Key A was neither muted nor charged for the rotation.
        assert adapter._key_pool is not None
        assert adapter._key_pool.can_serve_role(None)
    finally:
        reset_upstream_limiter()


async def test_every_key_saturated_surfaces_upstream_saturated(monkeypatch):
    limiter = _limiter(initial_limit=1, acquire_timeout=0.01)
    reset_upstream_limiter(limiter)
    try:
        adapter = _adapter(api_keys=[KEY_A, KEY_B])
        for key in (KEY_A, KEY_B):
            await limiter.acquire(PROVIDER, key, base_url=REMOTE)

        async def never_called(**_kwargs):  # pragma: no cover - must not run
            raise AssertionError("no request may be sent when every key is saturated")

        monkeypatch.setattr(adapter.http, "json_post", never_called)

        with pytest.raises(UpstreamSaturated):
            await adapter.chat_completion([{"role": "user", "content": "x"}])
    finally:
        reset_upstream_limiter()


# The ``async with`` guard is how anthropic/claude/gemini hold their slot, so
# its unwind paths are worth pinning independently of any one adapter.


async def test_slot_guard_releases_across_a_whole_generator(installed_limiter):
    async def produce():
        async with upstream_slot(PROVIDER, KEY_A, base_url=REMOTE):
            for index in range(3):
                yield index

    seen = []
    async for index in produce():
        seen.append(index)
        assert _state(installed_limiter)["in_flight"] == 1

    assert seen == [0, 1, 2]
    assert _state(installed_limiter)["in_flight"] == 0
    assert _state(installed_limiter)["successes_since_probe"] == 1


async def test_slot_guard_releases_when_its_generator_is_closed_mid_stream(installed_limiter):
    async def produce():
        async with upstream_slot(PROVIDER, KEY_A, base_url=REMOTE):
            for index in range(100):
                yield index

    gen = produce()
    await gen.__anext__()
    assert _state(installed_limiter)["in_flight"] == 1

    await gen.aclose()
    assert _state(installed_limiter)["in_flight"] == 0
    # A generator close says nothing about the concurrency level: neutral.
    assert _state(installed_limiter)["successes_since_probe"] == 0


async def test_slot_guard_feeds_a_429_to_the_controller(installed_limiter):
    limiter = installed_limiter
    (await _acquire(limiter)).release(status_code=200)  # seed the bucket
    assert _state(limiter)["limit"] == 1

    with pytest.raises(aiohttp.ClientResponseError):
        async with upstream_slot(PROVIDER, KEY_A, base_url=REMOTE):
            raise aiohttp.ClientResponseError(
                request_info=None, history=(), status=429, message="rate limited"
            )

    assert _state(limiter)["in_flight"] == 0
    assert _state(limiter)["limit"] == 1  # floor
    assert _state(limiter)["successes_since_probe"] == 0


async def test_saturation_does_not_count_against_endpoint_health():
    """Admission control this gateway imposed must not open the endpoint's circuit."""
    from routing.endpoint_health import EndpointHealthRegistry

    registry = EndpointHealthRegistry()
    endpoint_id = "glm-4.7:zai-api"
    for _ in range(10):
        registry.record_failure(
            endpoint_id,
            reason="chat_exception",
            exc=UpstreamSaturated("no slot"),
        )

    assert registry.allow_request(endpoint_id)
