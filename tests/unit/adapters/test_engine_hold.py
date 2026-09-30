"""Unit tests for the engine hold: the gateway's queue in front of a local engine.

A dispatch that carries an ``EngineHold`` is sent to its local engine only while
fewer than the hold's limit of that engine's held dispatches have gone without a
first token, and waits in the limiter otherwise. These cover the line itself, how
a wait ends (a place, a deadline, the acquire timeout, a cancellation), and the
adapter wiring that gives a place up at the engine's first output.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import contextmanager

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.dispatch_watch import report_first_token
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.adapters.upstream_limiter import (
    EngineHold,
    UpstreamConcurrencyLimiter,
    UpstreamQueueWaitExpired,
    UpstreamSaturated,
    _EngineGate,
    reset_upstream_limiter,
)
from serving.http import AsyncHTTPClient
from serving.utils import context as req_ctx

PROVIDER = "vllm"
KEY = "EMPTY"
ENGINE = "http://localhost:18003/v1"
ENGINE_ADDRESS = "localhost:18003"
OTHER_ENGINE = "http://localhost:18004/v1"
REMOTE = "https://api.example.com/v1"


def _limiter(**kwargs) -> UpstreamConcurrencyLimiter:
    """Build a limiter with small, explicit tunables (never reads the env)."""
    defaults = {"initial_limit": 2, "max_limit": 4, "acquire_timeout": 5.0}
    return UpstreamConcurrencyLimiter(**{**defaults, **kwargs})


@contextmanager
def _held(hold: EngineHold | None, *, deadline_in: float | None = None):
    """Push a dispatch's hold, and optionally its queue deadline, as FixedRouter does."""
    deadline = None if deadline_in is None else time.monotonic() + deadline_in
    with req_ctx.push(
        **{req_ctx.UPSTREAM_ENGINE_HOLD: hold, req_ctx.UPSTREAM_QUEUE_DEADLINE: deadline}
    ):
        yield


async def _send(
    limiter: UpstreamConcurrencyLimiter,
    hold: EngineHold | None,
    *,
    base_url: str = ENGINE,
    deadline_in: float | None = None,
):
    with _held(hold, deadline_in=deadline_in):
        return await limiter.acquire(PROVIDER, KEY, base_url=base_url)


def _line(limiter: UpstreamConcurrencyLimiter, address: str = ENGINE_ADDRESS) -> dict[str, int]:
    return limiter.engine_snapshot()[address]


async def _settle() -> None:
    """Let woken waiters resume."""
    for _ in range(3):
        await asyncio.sleep(0)


# ------------------------------------------------------------------ the line


async def test_a_dispatch_without_a_hold_goes_straight_to_its_engine():
    limiter = _limiter()

    slots = [await _send(limiter, None) for _ in range(5)]

    assert all(not slot.held for slot in slots)
    assert limiter.engine_snapshot() == {}


async def test_held_dispatches_go_while_the_engine_has_room():
    limiter = _limiter()

    first = await _send(limiter, EngineHold(2))
    second = await _send(limiter, EngineHold(2))

    assert first.held and second.held
    assert _line(limiter) == {"pending": 2, "waiting": 0}


async def test_a_dispatch_past_the_limit_waits_for_a_first_token():
    limiter = _limiter()
    first_hold = EngineHold(1)
    first = await _send(limiter, first_hold)

    second = asyncio.ensure_future(_send(limiter, EngineHold(1)))
    await _settle()
    assert not second.done()
    assert _line(limiter) == {"pending": 1, "waiting": 1}

    first_hold.on_first_token()
    await _settle()

    assert second.done()
    assert _line(limiter) == {"pending": 1, "waiting": 0}
    # The first stream is still open; ending it gives nothing back a second time.
    first.release(status_code=200)
    assert _line(limiter) == {"pending": 1, "waiting": 0}
    (await second).release(status_code=200)
    assert _line(limiter) == {"pending": 0, "waiting": 0}


async def test_a_stream_that_ends_before_a_first_token_frees_its_place():
    limiter = _limiter()
    first = await _send(limiter, EngineHold(1))
    second = asyncio.ensure_future(_send(limiter, EngineHold(1)))
    await _settle()

    first.release(status_code=0)
    await _settle()

    assert second.done()
    assert _line(limiter)["pending"] == 1
    (await second).release(status_code=200)


async def test_held_dispatches_go_in_arrival_order():
    limiter = _limiter()
    hold = EngineHold(1)
    await _send(limiter, hold)
    order: list[str] = []
    holds = {name: EngineHold(1) for name in ("a", "b", "c")}

    async def send(name: str) -> None:
        await _send(limiter, holds[name])
        order.append(name)

    tasks = []
    for name in ("a", "b", "c"):
        tasks.append(asyncio.ensure_future(send(name)))
        await _settle()

    hold.on_first_token()
    await _settle()
    holds["a"].on_first_token()
    await _settle()
    holds["b"].on_first_token()
    await _settle()

    assert order == ["a", "b", "c"]
    await asyncio.gather(*tasks)


async def test_a_new_dispatch_never_jumps_a_held_one():
    limiter = _limiter()
    ahead = EngineHold(1)
    await _send(limiter, ahead)
    waiting = asyncio.ensure_future(_send(limiter, EngineHold(1)))
    await _settle()

    # The place frees and the new arrival comes in the same tick: the one that
    # was already waiting gets it.
    ahead.on_first_token()
    late = asyncio.ensure_future(_send(limiter, EngineHold(1)))
    await _settle()

    assert waiting.done()
    assert not late.done()
    assert _line(limiter) == {"pending": 1, "waiting": 1}
    late.cancel()


async def test_raising_the_limit_lets_the_line_in_first():
    """A new limit applies to the requests already waiting, ahead of the one bringing it."""
    limiter = _limiter()
    await _send(limiter, EngineHold(1))
    waiting = asyncio.ensure_future(_send(limiter, EngineHold(1)))
    await _settle()

    arriving = asyncio.ensure_future(_send(limiter, EngineHold(2)))
    await _settle()

    assert waiting.done()
    assert not arriving.done()
    assert _line(limiter) == {"pending": 2, "waiting": 1}
    arriving.cancel()


async def test_a_lowered_limit_holds_arrivals_until_the_engine_drains_below_it():
    limiter = _limiter()
    holds = [EngineHold(3) for _ in range(3)]
    for hold in holds:
        await _send(limiter, hold)

    arriving = asyncio.ensure_future(_send(limiter, EngineHold(1)))
    await _settle()
    holds[0].on_first_token()
    holds[1].on_first_token()
    await _settle()
    assert not arriving.done()

    holds[2].on_first_token()
    await _settle()
    assert arriving.done()
    assert _line(limiter) == {"pending": 1, "waiting": 0}


def test_a_waiter_that_gave_up_in_the_same_tick_holds_nobody_back():
    """Its task has not yet left the line; the room it gave up is still room."""

    async def arrive() -> bool:
        gate = _EngineGate(address=ENGINE_ADDRESS)
        gate.rebind_loop(asyncio.get_running_loop())
        gone: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        gone.cancel()
        gate.waiters.append(gone)
        return gate.admits(1)

    assert asyncio.run(arrive()) is True


async def test_a_place_is_given_up_only_once():
    limiter = _limiter()
    hold = EngineHold(2)
    slot = await _send(limiter, hold)
    other = await _send(limiter, EngineHold(2))
    assert _line(limiter)["pending"] == 2

    hold.on_first_token()
    hold.on_first_token()
    slot.release(status_code=200)
    slot.release(status_code=200)

    assert _line(limiter)["pending"] == 1
    other.release(status_code=200)
    assert _line(limiter)["pending"] == 0


async def test_each_engine_has_its_own_line():
    limiter = _limiter()

    await _send(limiter, EngineHold(1))
    other = await _send(limiter, EngineHold(1), base_url=OTHER_ENGINE)

    assert other.held
    assert _line(limiter) == {"pending": 1, "waiting": 0}
    assert _line(limiter, "localhost:18004") == {"pending": 1, "waiting": 0}


async def test_two_routes_to_one_engine_share_its_line():
    """The engine is what queues, whatever path each route's base URL carries."""
    limiter = _limiter()
    await _send(limiter, EngineHold(1), base_url="http://localhost:18003/v1")

    second = asyncio.ensure_future(
        _send(limiter, EngineHold(1), base_url="http://LOCALHOST:18003/")
    )
    await _settle()

    assert not second.done()
    assert list(limiter.engine_snapshot()) == [ENGINE_ADDRESS]
    second.cancel()


async def test_a_remote_endpoint_is_never_held():
    """A hold is for an engine's own queue; a remote endpoint has the AIMD limit."""
    limiter = _limiter(initial_limit=1)

    slot = await _send(limiter, EngineHold(1), base_url=REMOTE)

    assert limiter.engine_snapshot() == {}
    assert len(limiter.snapshot()) == 1
    slot.release(status_code=200)


async def test_the_hold_works_with_the_limiter_switched_off():
    """The switch governs the AIMD limit; a hold is asked for per model."""
    limiter = _limiter(enabled=False)
    await _send(limiter, EngineHold(1))

    second = asyncio.ensure_future(_send(limiter, EngineHold(1)))
    await _settle()

    assert not second.done()
    second.cancel()


def test_a_hold_limit_is_a_whole_number_of_at_least_one():
    for bad in (0, -1, True, 1.5, "2", None):
        with pytest.raises(ValueError, match="at least 1"):
            EngineHold(bad)  # type: ignore[arg-type]
    assert EngineHold(3).limit == 3


# ------------------------------------------------------------ ending a wait


async def test_a_held_dispatch_leaves_at_its_queue_deadline():
    limiter = _limiter()
    await _send(limiter, EngineHold(1))

    started = time.monotonic()
    with pytest.raises(UpstreamQueueWaitExpired, match="Local engine 'localhost:18003'"):
        await _send(limiter, EngineHold(1), deadline_in=0.05)

    assert 0.04 <= time.monotonic() - started < 2.0
    # It left the line holding nothing, and nothing was counted for it.
    assert _line(limiter) == {"pending": 1, "waiting": 0}


async def test_a_held_dispatch_waits_out_a_deadline_past_the_acquire_timeout():
    """The hold replaces the engine's own queue, where the wait was the whole wait."""
    limiter = _limiter(acquire_timeout=0.02)
    await _send(limiter, EngineHold(1))

    started = time.monotonic()
    with pytest.raises(UpstreamQueueWaitExpired):
        await _send(limiter, EngineHold(1), deadline_in=0.15)

    assert time.monotonic() - started >= 0.14


async def test_the_deadline_error_is_the_gateways_own_saturation():
    """Key rotation, the breaker exemption and the offload order all read the base class."""
    limiter = _limiter()
    await _send(limiter, EngineHold(1))

    with pytest.raises(UpstreamSaturated) as excinfo:
        await _send(limiter, EngineHold(1), deadline_in=0.01)

    from routing.endpoint_health import _http_status_of

    assert isinstance(excinfo.value, UpstreamQueueWaitExpired)
    assert _http_status_of(excinfo.value) is None


async def test_a_held_dispatch_with_nowhere_else_to_go_is_sent_after_the_acquire_timeout(caplog):
    limiter = _limiter(acquire_timeout=0.05)
    await _send(limiter, EngineHold(1))

    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        slot = await _send(limiter, EngineHold(1))

    assert time.monotonic() - started >= 0.04
    assert slot.held
    # Over the limit: the engine queues it, as it would with no hold.
    assert _line(limiter) == {"pending": 2, "waiting": 0}
    assert [r for r in caplog.records if r.getMessage() == "engine_hold_overflowed"]
    slot.release(status_code=200)
    assert _line(limiter)["pending"] == 1


async def test_a_spent_deadline_leaves_without_taking_a_place_in_line():
    limiter = _limiter()
    await _send(limiter, EngineHold(1))
    queued = asyncio.ensure_future(_send(limiter, EngineHold(1)))
    await _settle()

    with pytest.raises(UpstreamQueueWaitExpired):
        await _send(limiter, EngineHold(1), deadline_in=-1.0)

    assert _line(limiter) == {"pending": 1, "waiting": 1}
    queued.cancel()


async def test_a_spent_deadline_still_takes_a_free_place():
    """The deadline bounds waiting, never admission."""
    limiter = _limiter()

    slot = await _send(limiter, EngineHold(1), deadline_in=-1.0)

    assert slot.held
    assert _line(limiter)["pending"] == 1


async def test_a_cancelled_dispatch_leaves_the_line_holding_nothing():
    limiter = _limiter()
    ahead = EngineHold(1)
    await _send(limiter, ahead)
    leaving = asyncio.ensure_future(_send(limiter, EngineHold(1)))
    behind = asyncio.ensure_future(_send(limiter, EngineHold(1)))
    await _settle()

    leaving.cancel()
    await _settle()
    assert _line(limiter) == {"pending": 1, "waiting": 1}

    ahead.on_first_token()
    await _settle()
    assert behind.done()
    assert _line(limiter) == {"pending": 1, "waiting": 0}


def test_a_place_granted_as_its_waiter_gave_up_goes_back():
    """The grant and the timeout can land in the same tick; the place must not leak."""

    async def race() -> dict[str, int]:
        gate = _EngineGate(address=ENGINE_ADDRESS)
        gate.rebind_loop(asyncio.get_running_loop())
        gate.grant()
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        gate.waiters.append(waiter)
        gate.give_back(gate.epoch)  # the engine answers: the waiter is granted
        assert waiter.done() and gate.pending == 1
        gate.abandon(waiter)  # ...but its wait had already run out
        return {"pending": gate.pending, "waiting": len(gate.waiters)}

    assert asyncio.run(race()) == {"pending": 0, "waiting": 0}


async def test_a_watch_hears_the_hold_and_then_the_send():
    """The engine's first-token wait must not run while the request is still here."""

    class _Watch:
        def __init__(self) -> None:
            self.events: list[str] = []

        def on_queued(self) -> None:
            self.events.append("queued")

        def on_sent(self) -> None:
            self.events.append("sent")

    limiter = _limiter()
    ahead = EngineHold(1)
    await _send(limiter, ahead)
    watch = _Watch()

    async def watched():
        with req_ctx.push(**{req_ctx.UPSTREAM_DISPATCH_WATCH: watch}):
            return await _send(limiter, EngineHold(1))

    waiting = asyncio.ensure_future(watched())
    await _settle()
    assert watch.events == ["queued"]
    ahead.on_first_token()
    await waiting

    assert watch.events == ["queued", "sent"]


def test_a_line_outliving_its_event_loop_starts_over():
    """Places taken on a loop that is gone can never be given back; drop them."""
    limiter = _limiter()
    stale: list[object] = []

    async def take_and_abandon() -> None:
        stale.append(await _send(limiter, EngineHold(1)))  # deliberately never released

    asyncio.run(take_and_abandon())
    assert _line(limiter)["pending"] == 1

    async def use_fresh_loop() -> dict[str, int]:
        slot = await _send(limiter, EngineHold(1))
        # A place from the old loop, given back now, is not taken off this count.
        stale[0].release(status_code=200)  # type: ignore[attr-defined]
        line = _line(limiter)
        slot.release(status_code=200)
        return line

    assert asyncio.run(use_fresh_loop()) == {"pending": 1, "waiting": 0}


# ------------------------------------------------------------ adapter wiring


def _chunk(delta: dict, finish_reason: str | None = None) -> str:
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "local-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _local_adapter() -> OpenAICompatAdapter:
    adapter = OpenAICompatAdapter(
        ModelConfig(
            id="local-model",
            name="Local model",
            provider=PROVIDER,
            base_url=ENGINE,
            api_key=KEY,
            provider_model_id="local-model",
            processor="default",
            supported_params=["temperature", "max_tokens"],
        )
    )
    # Its own client, so the stub below dies with the test (see test_upstream_limiter).
    adapter.http = AsyncHTTPClient()
    return adapter


@pytest.fixture
def installed_limiter():
    limiter = _limiter()
    reset_upstream_limiter(limiter)
    yield limiter
    reset_upstream_limiter()


async def test_an_engines_first_output_gives_the_place_up_mid_stream(
    installed_limiter, monkeypatch
):
    adapter = _local_adapter()
    rest = asyncio.Event()

    async def fake_stream_post(*_args, **_kwargs):
        yield _chunk({"role": "assistant"})
        yield _chunk({"content": "hel"})
        await rest.wait()
        yield _chunk({"content": "lo"}, finish_reason="stop")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(adapter.http, "stream_post", fake_stream_post)
    hold = EngineHold(1)
    seen: list[dict[str, int]] = []

    async def consume() -> None:
        with _held(hold):
            async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "hi"}]):
                if '"hel"' in chunk:
                    seen.append(dict(_line(installed_limiter)))
                    rest.set()

    await consume()

    # The place was free while the stream was still being generated...
    assert seen == [{"pending": 0, "waiting": 0}]
    # ...and ending the stream gave nothing back a second time.
    assert _line(installed_limiter) == {"pending": 0, "waiting": 0}


async def test_a_first_token_report_reaches_the_hold_and_a_broken_one_is_logged(caplog):
    class _Hold:
        def __init__(self, *, fail: bool = False) -> None:
            self.calls = 0
            self.fail = fail

        def on_first_token(self) -> None:
            self.calls += 1
            if self.fail:
                raise RuntimeError("broken hold")

    working, broken = _Hold(), _Hold(fail=True)
    with req_ctx.push(**{req_ctx.UPSTREAM_ENGINE_HOLD: working}):
        report_first_token()
    with req_ctx.push(**{req_ctx.UPSTREAM_ENGINE_HOLD: broken}):
        report_first_token()

    assert (working.calls, broken.calls) == (1, 1)
    failures = [r for r in caplog.records if r.getMessage() == "upstream_dispatch_watch_failed"]
    assert [getattr(r, "target", None) for r in failures] == [req_ctx.UPSTREAM_ENGINE_HOLD]
