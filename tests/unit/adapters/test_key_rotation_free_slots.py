"""A pooled route uses a sibling key's free slot before it queues, and a limit recovers in minutes.

Every caller started on key 0, so a request finding that key at its outbound
limit waited out the 30 s acquire timeout there while key 1 sat idle. And after
a burst of 429s the limit climbed back by one per 100 successes: about two hours
from 1 to 8 at one request at a time.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.key_pool import KeyPool
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.adapters.upstream_limiter import (
    UpstreamConcurrencyLimiter,
    key_fingerprint,
    reset_upstream_limiter,
)
from serving.utils import context as req_ctx

pytestmark = pytest.mark.unit

PROVIDER = "zai"
REMOTE = "https://api.example.com/v1"


@pytest.fixture
def limiter():
    installed = UpstreamConcurrencyLimiter(initial_limit=1, max_limit=8, acquire_timeout=3.0)
    reset_upstream_limiter(installed)
    try:
        yield installed
    finally:
        reset_upstream_limiter(None)


class _HeldHTTP:
    """Upstream calls that last until the test lets them finish, recording their keys."""

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.finish = asyncio.Event()

    async def json_post(self, url: str, *, json: Any, headers: dict[str, str], **_kw: Any):
        self.keys.append(headers["Authorization"][-1])
        await self.finish.wait()
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    async def stream_post(self, url: str, *, json: Any, headers: dict[str, str], **_kw: Any):
        self.keys.append(headers["Authorization"][-1])
        await self.finish.wait()
        yield 'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}'
        yield "data: [DONE]"


def _adapter(keys: list[str]) -> tuple[OpenAICompatAdapter, _HeldHTTP]:
    adapter = OpenAICompatAdapter(
        ModelConfig(id="m", name="m", provider=PROVIDER, base_url=REMOTE, api_keys=keys)
    )
    http = _HeldHTTP()
    adapter.http = http
    return adapter, http


async def _call(adapter: OpenAICompatAdapter, stream: bool, caller: str) -> None:
    with req_ctx.push(affinity_key=caller):
        if stream:
            async for _ in adapter.stream_chat_completion([{"role": "user", "content": "x"}]):
                pass
        else:
            await adapter.chat_completion([{"role": "user", "content": "x"}])


async def _until(condition, timeout: float = 1.0) -> None:
    async def wait() -> None:
        while not condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["non-streaming", "streaming"])
async def test_a_request_takes_an_idle_sibling_key_instead_of_queueing(limiter, stream):
    adapter, http = _adapter(["key-A", "key-B"])

    first = asyncio.create_task(_call(adapter, stream, "caller-1"))
    await _until(lambda: http.keys == ["A"])
    second = asyncio.create_task(_call(adapter, stream, "caller-2"))

    # Sent at once on key B -- not after key A's acquire timeout.
    await _until(lambda: http.keys == ["A", "B"])
    assert limiter.snapshot()[(PROVIDER, key_fingerprint("key-A"))]["waiting"] == 0
    http.finish.set()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_with_every_key_full_a_request_queues_on_its_key(limiter):
    adapter, http = _adapter(["key-A", "key-B"])
    running = [asyncio.create_task(_call(adapter, False, f"caller-{n}")) for n in (1, 2)]
    await _until(lambda: sorted(http.keys) == ["A", "B"])

    third = asyncio.create_task(_call(adapter, False, "caller-1"))
    await _until(lambda: limiter.snapshot()[(PROVIDER, key_fingerprint("key-A"))]["waiting"] == 1)

    http.finish.set()
    await asyncio.gather(*running, third)
    assert http.keys[-1] == "A"  # caller-1's key, once it came free


@pytest.mark.asyncio
async def test_a_bound_caller_borrows_a_sibling_and_keeps_its_own_key(limiter):
    adapter, http = _adapter(["key-A", "key-B"])
    pool = adapter._key_pool
    assert pool is not None
    # caller-1 is bound to key A; caller-2 holds key A's only slot.
    pool.acquire("caller-1")
    busy = asyncio.create_task(_call(adapter, False, "caller-2"))
    await _until(lambda: http.keys == ["A"])

    borrowed = asyncio.create_task(_call(adapter, False, "caller-1"))
    await _until(lambda: http.keys == ["A", "B"])
    http.finish.set()
    await asyncio.gather(busy, borrowed)

    assert pool._affinity["caller-1"].key_index == 0  # still key A


# ---------------------------------------------------------------- the key pool


def test_acquire_with_offers_keys_in_the_pools_order_and_binds_a_new_caller():
    pool = KeyPool(["k0", "k1", "k2"], provider_label="p")
    offered: list[str] = []

    def admit(key: str) -> str | None:
        offered.append(key)
        return "slot" if key == "k2" else None

    key, lease, admitted = pool.acquire_with("caller", admit)

    assert (key, lease.key_index, admitted) == ("k2", 2, "slot")
    assert offered == ["k0", "k1", "k2"]
    assert pool._affinity["caller"].key_index == 2


def test_acquire_with_offers_the_bound_key_first_and_keeps_the_binding():
    pool = KeyPool(["k0", "k1", "k2"], provider_label="p")
    pool._affinity.clear()
    _, lease = pool.acquire("caller")  # bound to k0
    offered: list[str] = []

    def admit(key: str) -> str | None:
        offered.append(key)
        return "slot" if key == "k1" else None

    key, _, _ = pool.acquire_with("caller", admit)

    assert key == "k1"
    assert offered == ["k0", "k1"]
    assert pool._affinity["caller"].key_index == lease.key_index == 0


def test_acquire_with_changes_nothing_when_no_key_admits():
    pool = KeyPool(["k0", "k1"], provider_label="p")

    assert pool.acquire_with("caller", lambda _key: None) is None
    assert pool.affinity_count() == 0
    assert all(state.request_count == 0 for state in pool._keys)


def test_acquire_with_skips_tried_and_muted_keys():
    pool = KeyPool(["k0", "k1", "k2"], provider_label="p")
    pool._keys[1].cooldown_until = float("inf")
    offered: list[str] = []

    def admit(key: str) -> str | None:
        offered.append(key)
        return None

    pool.acquire_with("caller", admit, exclude={0})

    assert offered == ["k2"]


# ---------------------------------------------------------------- the limiter


@pytest.mark.asyncio
async def test_try_acquire_takes_a_free_slot_and_never_waits():
    limiter = UpstreamConcurrencyLimiter(initial_limit=1, max_limit=1, acquire_timeout=1.0)

    slot = limiter.try_acquire(PROVIDER, "k", base_url=REMOTE)
    assert slot is not None and slot.held
    assert limiter.try_acquire(PROVIDER, "k", base_url=REMOTE) is None

    slot.release(status_code=200)
    again = limiter.try_acquire(PROVIDER, "k", base_url=REMOTE)
    assert again is not None
    again.release(status_code=200)


@pytest.mark.asyncio
async def test_try_acquire_does_not_jump_the_queue():
    limiter = UpstreamConcurrencyLimiter(initial_limit=1, max_limit=1, acquire_timeout=1.0)
    held = await limiter.acquire(PROVIDER, "k", base_url=REMOTE)
    waiter = asyncio.create_task(limiter.acquire(PROVIDER, "k", base_url=REMOTE))
    await asyncio.sleep(0)

    held.release(status_code=200)  # the slot goes to the waiter, not to a newcomer

    assert limiter.try_acquire(PROVIDER, "k", base_url=REMOTE) is None
    (await waiter).release(status_code=200)


@pytest.mark.asyncio
async def test_try_acquire_leaves_a_self_hosted_engine_to_acquire():
    limiter = UpstreamConcurrencyLimiter()

    assert limiter.try_acquire(PROVIDER, "k", base_url="http://localhost:8000/v1") is None


def _bucket(limiter: UpstreamConcurrencyLimiter, key: str = "k"):
    return limiter._buckets[(PROVIDER, key_fingerprint(key))]


@pytest.mark.asyncio
async def test_after_a_burst_of_429s_the_limit_recovers_by_time_not_by_count():
    limiter = UpstreamConcurrencyLimiter(
        initial_limit=8, max_limit=8, probe_success_interval=100, probe_interval_s=60.0
    )
    for _ in range(7):
        (await limiter.acquire(PROVIDER, "k", base_url=REMOTE)).release(status_code=429)
    bucket = _bucket(limiter)
    assert bucket.limit == 1

    # A success within the minute changes nothing; one after it probes.
    (await limiter.acquire(PROVIDER, "k", base_url=REMOTE)).release(status_code=200)
    assert bucket.limit == 1
    for expected in range(2, 9):
        bucket.last_change -= 61
        (await limiter.acquire(PROVIDER, "k", base_url=REMOTE)).release(status_code=200)
        assert bucket.limit == expected


@pytest.mark.asyncio
async def test_a_probe_the_provider_refuses_doubles_the_wait_for_the_next():
    limiter = UpstreamConcurrencyLimiter(
        initial_limit=2, max_limit=8, probe_success_interval=100, probe_interval_s=60.0
    )
    (await limiter.acquire(PROVIDER, "k", base_url=REMOTE)).release(status_code=200)
    bucket = _bucket(limiter)
    bucket.last_change -= 61
    (await limiter.acquire(PROVIDER, "k", base_url=REMOTE)).release(status_code=200)
    assert (bucket.limit, bucket.probing) == (3, True)

    (await limiter.acquire(PROVIDER, "k", base_url=REMOTE)).release(status_code=429)
    assert (bucket.limit, bucket.probe_backoff) == (2, 2)

    bucket.last_change -= 61  # one interval is no longer enough
    (await limiter.acquire(PROVIDER, "k", base_url=REMOTE)).release(status_code=200)
    assert bucket.limit == 2
    bucket.last_change -= 60
    (await limiter.acquire(PROVIDER, "k", base_url=REMOTE)).release(status_code=200)
    assert bucket.limit == 3

    # That probe held until the next one: the wait is one interval again.
    bucket.last_change -= 121
    (await limiter.acquire(PROVIDER, "k", base_url=REMOTE)).release(status_code=200)
    assert (bucket.limit, bucket.probe_backoff) == (4, 1)
