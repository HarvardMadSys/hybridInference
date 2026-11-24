"""Unit tests for registered engine queues: a local engine's own queue in the gateway.

An engine registered with the limiter (``set_engine_queues``) counts every dispatch
it is sent against its own limit, whatever the dispatch carries, and a place is
given back when the adapter reports the engine's first output through the slot it
holds. These cover counting without a hold, how a place is given back and how a
wait ends, registration and its refusals, the counters the admin view reads, and
the adapter wiring.
"""

from __future__ import annotations

import asyncio
import json
import math
import time

import pytest

from serving.adapters.anthropic import AnthropicAdapter
from serving.adapters.base import ModelConfig
from serving.adapters.dispatch_watch import report_first_token
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.adapters.upstream_limiter import (
    ENGINE_WAIT_BUCKETS_S,
    EngineHold,
    EngineQueueConflict,
    EngineQueueUnavailable,
    UpstreamConcurrencyLimiter,
    UpstreamQueueWaitExpired,
    _EngineGate,
    _EngineQueueStats,
    dispatch_queue_context,
    reset_upstream_limiter,
)
from serving.http import AsyncHTTPClient
from serving.utils import context as req_ctx, workers

PROVIDER = "vllm"
KEY = "EMPTY"
OWNER = "model-a"
ENGINE = "http://localhost:18003/v1"
ENGINE_ADDRESS = "localhost:18003"
#: Another base URL that reaches the same server.
SAME_ENGINE = "http://localhost:18003/other/v1"
OTHER_ENGINE = "http://localhost:18004/v1"
OTHER_ADDRESS = "localhost:18004"


def _limiter(**kwargs) -> UpstreamConcurrencyLimiter:
    """Build a limiter with small, explicit tunables (never reads the env)."""
    defaults = {"initial_limit": 2, "max_limit": 4, "acquire_timeout": 5.0}
    return UpstreamConcurrencyLimiter(**{**defaults, **kwargs})


def _registered(limit: int = 1, **kwargs) -> UpstreamConcurrencyLimiter:
    """Build a limiter whose engine at :data:`ENGINE` has a queue of *limit*."""
    limiter = _limiter(**kwargs)
    limiter.set_engine_queues(OWNER, {ENGINE: limit})
    return limiter


async def _send(
    limiter: UpstreamConcurrencyLimiter,
    *,
    base_url: str = ENGINE,
    deadline_in: float | None = None,
    hold: EngineHold | None = None,
):
    """Dispatch once, with the queue terms a router pushes."""
    deadline = None if deadline_in is None else time.monotonic() + deadline_in
    with req_ctx.push(**dispatch_queue_context(deadline=deadline, hold=hold)):
        return await limiter.acquire(PROVIDER, KEY, base_url=base_url)


def _queue(limiter: UpstreamConcurrencyLimiter, address: str = ENGINE_ADDRESS) -> dict:
    return limiter.engine_snapshot()[address]


def _line(limiter: UpstreamConcurrencyLimiter, address: str = ENGINE_ADDRESS) -> dict[str, int]:
    state = _queue(limiter, address)
    return {"pending": state["pending"], "waiting": state["waiting"]}


async def _settle() -> None:
    """Let woken waiters resume."""
    for _ in range(3):
        await asyncio.sleep(0)


# ------------------------------------------------------------- counting


async def test_a_registered_engine_counts_dispatches_that_carry_no_hold():
    limiter = _registered(limit=1)

    first = await _send(limiter)
    behind = asyncio.ensure_future(_send(limiter))
    await _settle()

    assert first.held
    assert _line(limiter) == {"pending": 1, "waiting": 1}

    first.first_token()
    second = await behind

    assert _line(limiter) == {"pending": 1, "waiting": 0}
    second.release(status_code=200)
    assert _line(limiter) == {"pending": 0, "waiting": 0}


async def test_an_engine_nobody_registered_still_takes_unheld_dispatches_straight():
    limiter = _limiter()
    limiter.set_engine_queues(OWNER, {OTHER_ENGINE: 1})

    slots = [await _send(limiter) for _ in range(3)]

    assert not any(slot.held for slot in slots)
    assert ENGINE_ADDRESS not in limiter.engine_snapshot()


async def test_the_registered_limit_applies_over_a_held_dispatchs_own():
    limiter = _registered(limit=1)

    first = await _send(limiter, hold=EngineHold(5))
    behind = asyncio.ensure_future(_send(limiter, hold=EngineHold(5)))
    await _settle()

    assert _line(limiter) == {"pending": 1, "waiting": 1}
    assert _queue(limiter)["limit"] == 1
    first.release(status_code=200)
    (await behind).release(status_code=200)


async def test_two_base_urls_to_one_server_share_its_registered_queue():
    limiter = _registered(limit=1)

    first = await _send(limiter, base_url=SAME_ENGINE)
    behind = asyncio.ensure_future(_send(limiter))
    await _settle()

    assert _line(limiter) == {"pending": 1, "waiting": 1}
    first.release(status_code=200)
    (await behind).release(status_code=200)


async def test_each_registered_engine_has_its_own_line():
    limiter = _limiter()
    limiter.set_engine_queues(OWNER, {ENGINE: 1, OTHER_ENGINE: 1})

    here = await _send(limiter)
    there = await _send(limiter, base_url=OTHER_ENGINE)

    assert _line(limiter) == {"pending": 1, "waiting": 0}
    assert _line(limiter, OTHER_ADDRESS) == {"pending": 1, "waiting": 0}
    here.release(status_code=200)
    there.release(status_code=200)


# ------------------------------------------------ giving a place back


async def test_the_slot_gives_the_place_up_at_the_first_token_and_only_once():
    limiter = _registered(limit=1)
    slot = await _send(limiter)

    slot.first_token()

    assert not slot.held
    assert _line(limiter) == {"pending": 0, "waiting": 0}
    slot.first_token()
    slot.release(status_code=200)
    state = _queue(limiter)
    assert (state["released_on_first_token"], state["released_answered"]) == (1, 0)
    assert state["pending"] == 0


async def test_a_hold_and_the_slot_give_up_one_place_between_them():
    limiter = _registered(limit=1)
    hold = EngineHold(1)
    slot = await _send(limiter, hold=hold)

    hold.on_first_token()
    slot.first_token()
    slot.release(status_code=200)

    state = _queue(limiter)
    assert (state["pending"], state["released_on_first_token"]) == (0, 1)


async def test_a_request_answered_whole_holds_its_place_to_the_end_and_is_counted():
    limiter = _registered(limit=1)
    slot = await _send(limiter)

    assert _line(limiter)["pending"] == 1
    slot.release(status_code=200)

    state = _queue(limiter)
    assert (state["pending"], state["released_answered"]) == (0, 1)


@pytest.mark.parametrize("status_code", [None, 0, 429, 500])
async def test_a_request_that_ended_before_answering_is_counted_apart(status_code):
    limiter = _registered(limit=1)
    slot = await _send(limiter)

    slot.release(status_code=status_code)

    state = _queue(limiter)
    assert (state["pending"], state["released_unanswered"], state["released_answered"]) == (
        0,
        1,
        0,
    )


async def test_a_first_token_report_with_the_slot_lets_the_next_request_go():
    limiter = _registered(limit=1)
    slot = await _send(limiter)
    behind = asyncio.ensure_future(_send(limiter))
    await _settle()

    report_first_token(slot)

    (await behind).release(status_code=200)
    slot.release(status_code=200)
    assert _queue(limiter)["released_on_first_token"] == 1


async def test_a_broken_slot_report_is_logged_and_ignored(caplog):
    class _BrokenSlot:
        def first_token(self) -> None:
            raise RuntimeError("broken slot")

    report_first_token(_BrokenSlot())  # type: ignore[arg-type]

    failures = [r for r in caplog.records if r.getMessage() == "upstream_dispatch_watch_failed"]
    assert [getattr(r, "target", None) for r in failures] == ["slot"]


async def test_a_slot_without_a_place_at_an_engine_ignores_the_first_token():
    limiter = _limiter()
    remote = await _send(limiter, base_url="https://api.example.com/v1")

    remote.first_token()

    assert remote.held
    remote.release(status_code=200)
    assert not remote.held


# ------------------------------------------------------ how a wait ends


async def test_a_spent_deadline_leaves_a_full_engine_at_once():
    limiter = _registered(limit=1)
    first = await _send(limiter)

    with pytest.raises(UpstreamQueueWaitExpired):
        await _send(limiter, deadline_in=-1)

    state = _queue(limiter)
    assert (state["expired"], state["waits"], state["waiting"]) == (1, 0, 0)
    first.release(status_code=200)


async def test_a_spent_deadline_still_takes_a_free_place():
    limiter = _registered(limit=1)

    slot = await _send(limiter, deadline_in=-1)

    assert slot.held
    assert _queue(limiter)["sent_at_once"] == 1
    slot.release(status_code=200)


async def test_a_waiter_leaves_unsent_at_its_deadline_and_its_wait_is_counted():
    limiter = _registered(limit=1)
    first = await _send(limiter)

    with pytest.raises(UpstreamQueueWaitExpired):
        await _send(limiter, deadline_in=0.05)

    state = _queue(limiter)
    assert (state["expired"], state["waits"]) == (1, 1)
    assert 0.04 <= state["wait_seconds_max"] < 1.0
    assert sum(bucket["count"] for bucket in state["wait_buckets"]) == 1
    assert _line(limiter) == {"pending": 1, "waiting": 0}
    first.release(status_code=200)


async def test_a_waiter_with_no_deadline_is_sent_past_the_limit_after_the_acquire_timeout():
    limiter = _registered(limit=1, acquire_timeout=0.05)
    first = await _send(limiter)

    second = await _send(limiter)

    assert second.held
    state = _queue(limiter)
    assert (state["overflowed"], state["pending"], state["waits"]) == (1, 2, 1)
    first.release(status_code=200)
    second.release(status_code=200)


async def test_a_cancelled_waiter_is_counted_and_holds_nothing():
    limiter = _registered(limit=1)
    first = await _send(limiter)
    leaving = asyncio.ensure_future(_send(limiter))
    await _settle()

    leaving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leaving

    state = _queue(limiter)
    assert (state["cancelled"], state["waiting"], state["pending"]) == (1, 0, 1)
    first.release(status_code=200)
    assert _line(limiter) == {"pending": 0, "waiting": 0}


async def test_a_waiter_sent_when_its_turn_came_is_counted_with_its_wait():
    limiter = _registered(limit=1)
    first = await _send(limiter)
    behind = asyncio.ensure_future(_send(limiter, deadline_in=5))
    await asyncio.sleep(0.02)

    first.first_token()
    second = await behind

    state = _queue(limiter)
    assert (state["sent_at_once"], state["sent_after_wait"], state["waits"]) == (1, 1, 1)
    assert state["wait_seconds_total"] >= 0.01
    second.release(status_code=200)


@pytest.mark.parametrize(
    ("seconds", "bucket"),
    [
        (0.0, 0),
        (ENGINE_WAIT_BUCKETS_S[0], 0),
        (ENGINE_WAIT_BUCKETS_S[0] + 0.01, 1),
        (ENGINE_WAIT_BUCKETS_S[-1], len(ENGINE_WAIT_BUCKETS_S) - 1),
        (ENGINE_WAIT_BUCKETS_S[-1] + 1, len(ENGINE_WAIT_BUCKETS_S)),
    ],
)
def test_a_wait_is_counted_in_the_first_bucket_that_holds_it(seconds, bucket):
    stats = _EngineQueueStats()

    stats.waited(seconds)

    assert [index for index, count in enumerate(stats.wait_buckets) if count] == [bucket]
    labels = [entry["le_seconds"] for entry in stats.as_dict()["wait_buckets"]]
    assert labels == [*ENGINE_WAIT_BUCKETS_S, None]


# ---------------------------------------------------------- registration


def test_a_registered_engine_is_listed_before_any_traffic():
    limiter = _registered(limit=3)

    state = _queue(limiter)

    assert (state["limit"], state["registered"], state["owners"]) == (3, True, [OWNER])
    assert (state["held"], state["pending"], state["waiting"], state["sent_at_once"]) == (
        False,
        0,
        0,
        0,
    )
    assert limiter.engine_queue_limit(ENGINE) == 3
    assert limiter.engine_queue_limit(SAME_ENGINE) == 3
    assert limiter.engine_queue_limit(OTHER_ENGINE) is None


def test_owners_sharing_an_engine_must_agree_on_its_limit():
    limiter = _registered(limit=1)

    with pytest.raises(EngineQueueConflict, match=OWNER):
        limiter.set_engine_queues("model-b", {ENGINE: 2, OTHER_ENGINE: 1})

    # Nothing changed, not even the engine without a conflict.
    assert OTHER_ADDRESS not in limiter.engine_snapshot()
    assert _queue(limiter)["owners"] == [OWNER]
    limiter.set_engine_queues("model-b", {ENGINE: 1})
    assert _queue(limiter)["owners"] == [OWNER, "model-b"]


def test_one_owner_cannot_give_one_engine_two_limits():
    limiter = _limiter()

    with pytest.raises(EngineQueueConflict):
        limiter.set_engine_queues(OWNER, {ENGINE: 1, SAME_ENGINE: 2})

    assert limiter.engine_snapshot() == {}


@pytest.mark.parametrize("limit", [0, -1, 1.5, True, "1"])
def test_a_registered_limit_is_a_whole_number_of_at_least_one(limit):
    limiter = _limiter()

    with pytest.raises(ValueError, match="whole number"):
        limiter.set_engine_queues(OWNER, {ENGINE: limit})

    assert limiter.engine_snapshot() == {}


async def test_registering_again_replaces_the_set_and_a_raised_limit_lets_the_line_in():
    limiter = _registered(limit=1)
    first = await _send(limiter)
    behind = asyncio.ensure_future(_send(limiter))
    await _settle()

    limiter.set_engine_queues(OWNER, {ENGINE: 2, OTHER_ENGINE: 1})
    second = await behind

    # The count carried over, and the raised limit sent the waiter on at once.
    assert _line(limiter) == {"pending": 2, "waiting": 0}
    assert _queue(limiter, OTHER_ADDRESS)["registered"]
    limiter.set_engine_queues(OWNER, {OTHER_ENGINE: 1})
    assert not _queue(limiter)["registered"]
    first.release(status_code=200)
    second.release(status_code=200)


async def test_a_lowered_limit_holds_arrivals_until_the_engine_drains_below_it():
    limiter = _registered(limit=2)
    first = await _send(limiter)
    second = await _send(limiter)

    limiter.set_engine_queues(OWNER, {ENGINE: 1})
    behind = asyncio.ensure_future(_send(limiter))
    await _settle()
    assert _line(limiter) == {"pending": 2, "waiting": 1}

    first.release(status_code=200)
    await _settle()
    assert not behind.done()
    second.release(status_code=200)
    (await behind).release(status_code=200)


async def test_clearing_stops_counting_new_dispatches_and_lets_counted_ones_finish():
    limiter = _registered(limit=1)
    counted = await _send(limiter)

    limiter.clear_engine_queues(OWNER)
    uncounted = await _send(limiter)

    assert not uncounted.held
    state = _queue(limiter)
    assert (state["registered"], state["owners"], state["held"], state["pending"]) == (
        False,
        [],
        False,
        1,
    )
    counted.release(status_code=200)
    # Drained, and nobody registers or holds for it: the queue is forgotten.
    assert ENGINE_ADDRESS not in limiter.engine_snapshot()


async def test_clearing_sends_the_line_on_at_once_instead_of_behind_new_arrivals():
    limiter = _registered(limit=1)
    first = await _send(limiter)
    waiting = [asyncio.ensure_future(_send(limiter, deadline_in=5)) for _ in range(2)]
    await _settle()
    assert _line(limiter) == {"pending": 1, "waiting": 2}

    limiter.clear_engine_queues(OWNER)
    sent = [await waiter for waiter in waiting]

    # Past the old limit: nothing queues for this engine any more.
    state = _queue(limiter)
    assert (state["pending"], state["waiting"], state["sent_after_wait"]) == (3, 0, 2)
    for slot in (first, *sent):
        slot.release(status_code=200)
    assert ENGINE_ADDRESS not in limiter.engine_snapshot()


def test_an_idle_queue_is_forgotten_as_soon_as_it_is_cleared():
    limiter = _registered(limit=1)

    limiter.clear_engine_queues(OWNER)

    assert limiter.engine_snapshot() == {}


async def test_held_dispatches_keep_their_line_when_nobody_registers_the_engine():
    limiter = _registered(limit=1)
    first = await _send(limiter, hold=EngineHold(1))
    behind = asyncio.ensure_future(_send(limiter, hold=EngineHold(1)))
    await _settle()

    limiter.clear_engine_queues(OWNER)
    await _settle()

    # The offload's held dispatches still count against their own limit.
    state = _queue(limiter)
    assert (state["registered"], state["held"], state["waiting"]) == (False, True, 1)
    first.release(status_code=200)
    (await behind).release(status_code=200)
    # Nothing is in the queue any more, held or not: it is forgotten.
    assert ENGINE_ADDRESS not in limiter.engine_snapshot()


async def test_clearing_sends_unheld_waiters_on_though_held_traffic_used_the_queue_before():
    limiter = _registered(limit=1)
    # The offload's held traffic used this engine's queue, and is done.
    (await _send(limiter, hold=EngineHold(1))).release(status_code=200)
    first = await _send(limiter)
    behind = asyncio.ensure_future(_send(limiter, deadline_in=5))
    await _settle()
    assert _queue(limiter)["held"] is False

    limiter.clear_engine_queues(OWNER)
    second = await behind
    arrival = await _send(limiter)

    # The waiter went on at once, and the new arrival goes straight through.
    assert second.held and not arrival.held
    assert _line(limiter) == {"pending": 2, "waiting": 0}
    first.release(status_code=200)
    second.release(status_code=200)
    assert ENGINE_ADDRESS not in limiter.engine_snapshot()


async def test_clearing_sends_the_unheld_waiters_on_and_keeps_the_held_ones_in_line():
    limiter = _registered(limit=1)
    first = await _send(limiter)
    held_hold = EngineHold(1)
    held_waiter = asyncio.ensure_future(_send(limiter, hold=held_hold))
    unheld_waiter = asyncio.ensure_future(_send(limiter, deadline_in=5))
    await _settle()
    assert _queue(limiter)["held"] is True

    limiter.clear_engine_queues(OWNER)
    sent = await unheld_waiter

    state = _queue(limiter)
    assert (state["pending"], state["waiting"], state["held"]) == (2, 1, True)
    assert not held_waiter.done()
    first.release(status_code=200)
    sent.release(status_code=200)
    held = await held_waiter
    held_hold.on_first_token()
    held.release(status_code=200)
    assert ENGINE_ADDRESS not in limiter.engine_snapshot()


async def test_a_held_dispatch_keeps_an_unregistered_queue_until_it_is_done():
    limiter = _limiter()
    hold = EngineHold(1)
    slot = await _send(limiter, hold=hold)

    assert _queue(limiter)["held"] is True
    hold.on_first_token()

    # Its place is back and nothing waits: no hold in the queue any more.
    assert ENGINE_ADDRESS not in limiter.engine_snapshot()
    slot.release(status_code=200)


def test_a_held_place_granted_as_its_waiter_gave_up_goes_back_with_its_hold():
    """The grant and the timeout can land in the same tick; neither count may leak."""

    async def race() -> dict[str, int]:
        gate = _EngineGate(address=ENGINE_ADDRESS)
        gate.rebind_loop(asyncio.get_running_loop())
        ahead = gate.grant(held=True)
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        gate.join(waiter, held=True)
        ahead.release("first_token")  # the engine answers: the waiter is granted
        assert waiter.done() and (gate.pending, gate.held_pending) == (1, 1)
        gate.abandon(waiter)  # ...but its wait had already run out
        return {"pending": gate.pending, "held_pending": gate.held_pending, "held": gate.held}

    assert asyncio.run(race()) == {"pending": 0, "held_pending": 0, "held": False}


def test_an_engine_another_owner_registers_keeps_its_queue_when_one_clears():
    limiter = _registered(limit=1)
    limiter.set_engine_queues("model-b", {ENGINE: 1})

    limiter.clear_engine_queues(OWNER)

    assert _queue(limiter)["owners"] == ["model-b"]
    assert limiter.engine_queue_limit(ENGINE) == 1


def test_an_engine_queue_is_refused_while_several_workers_serve(monkeypatch):
    monkeypatch.setattr("serving.adapters.upstream_limiter.several_workers", lambda: True)
    limiter = _limiter()

    with pytest.raises(EngineQueueUnavailable, match="worker"):
        limiter.set_engine_queues(OWNER, {ENGINE: 1})

    assert limiter.engine_snapshot() == {}
    # Clearing is always allowed: it is how an owner gives its queues up.
    limiter.clear_engine_queues(OWNER)


@pytest.mark.parametrize(
    ("environment", "expected"),
    [({}, False), ({"WEB_CONCURRENCY": "1"}, False), ({"UVICORN_WORKERS": "2"}, True)],
)
def test_several_workers_reads_the_configured_worker_count(monkeypatch, environment, expected):
    for key in workers.WORKER_COUNT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(workers, "spawned_by_worker_manager", lambda: False)

    assert workers.several_workers() is expected


def test_several_workers_counts_a_managed_worker_process(monkeypatch):
    for key in workers.WORKER_COUNT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(workers, "spawned_by_worker_manager", lambda: True)

    assert workers.several_workers()


# ------------------------------------------------------ the queue terms


def test_the_queue_context_always_writes_both_terms():
    hold = EngineHold(1)

    assert dispatch_queue_context(deadline=None) == {
        req_ctx.UPSTREAM_QUEUE_DEADLINE: None,
        req_ctx.UPSTREAM_ENGINE_HOLD: None,
    }
    assert dispatch_queue_context(deadline=12.5, hold=hold) == {
        req_ctx.UPSTREAM_QUEUE_DEADLINE: 12.5,
        req_ctx.UPSTREAM_ENGINE_HOLD: hold,
    }


@pytest.mark.parametrize("deadline", [True, math.nan, math.inf, "soon"])
def test_the_queue_context_refuses_a_deadline_that_is_not_a_time(deadline):
    with pytest.raises(ValueError, match="deadline"):
        dispatch_queue_context(deadline=deadline)


# ------------------------------------------------------- adapter wiring


def _chunk(delta: dict[str, str], finish_reason: str | None = None) -> str:
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
    # Its own client, so the stubs below die with the test.
    adapter.http = AsyncHTTPClient()
    return adapter


@pytest.fixture
def installed():
    limiter = _registered(limit=1)
    reset_upstream_limiter(limiter)
    yield limiter
    reset_upstream_limiter()


async def test_the_adapter_gives_a_registered_engines_place_up_at_its_first_output(
    installed, monkeypatch
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
    seen: list[dict[str, int]] = []

    # No hold and no deadline in the request context: the engine's registration
    # alone counts the stream, and its slot alone gives the place up.
    async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "hi"}]):
        if '"hel"' in chunk:
            seen.append(_line(installed))
            rest.set()

    assert seen == [{"pending": 0, "waiting": 0}]
    state = _queue(installed)
    assert (
        state["sent_at_once"],
        state["released_on_first_token"],
        state["released_answered"],
    ) == (1, 1, 0)


def _sse(event: dict, *, terminated: bool = True) -> bytes:
    """One Anthropic SSE frame; an unterminated one lacks its closing blank line."""
    frame = f"event: {event['type']}\ndata: {json.dumps(event)}\n"
    return (frame + "\n" if terminated else frame).encode()


_MESSAGE_START = {"type": "message_start", "message": {"usage": {"input_tokens": 3}}}
_EMPTY_TEXT_START = {
    "type": "content_block_start",
    "index": 0,
    "content_block": {"type": "text", "text": ""},
}
_TEXT_DELTA = {
    "type": "content_block_delta",
    "index": 0,
    "delta": {"type": "text_delta", "text": "hi"},
}
_MESSAGE_STOP = {"type": "message_stop"}


async def _stream_passthrough(monkeypatch, limiter, frames: list[bytes]) -> list[int]:
    """Stream *frames* through the native /v1/messages passthrough to a local engine.

    Returns the engine's taken places as each chunk reached the caller. The route
    speaks the Anthropic protocol to a self-hosted engine, as SGLang and vLLM can.
    """

    class _Content:
        async def iter_any(self):
            for frame in frames:
                yield frame

    class _Response:
        status = 200
        content = _Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    class _Session:
        def post(self, *_args, **_kwargs):
            return _Response()

    async def fake_ensure_session(_self):
        return _Session()

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", fake_ensure_session)
    adapter = AnthropicAdapter(
        ModelConfig(
            id="local-model",
            name="Local model",
            provider="sglang",
            base_url=ENGINE,
            api_key=KEY,
            provider_model_id="local-model",
        )
    )
    body = {
        "model": "local-model",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    }
    pending: list[int] = []
    async for _chunk in adapter.stream_messages(body, request_id="req-1"):
        pending.append(_line(limiter)["pending"])
    return pending


async def test_the_messages_passthrough_frees_the_place_as_the_first_output_arrives(
    installed, monkeypatch
):
    frames = [_sse(_MESSAGE_START), _sse(_TEXT_DELTA), _sse(_MESSAGE_STOP)]

    pending = await _stream_passthrough(monkeypatch, installed, frames)

    # Free by the time the caller holds the frame with the first text in it,
    # not only once the caller asks for the next one.
    assert pending == [1, 0, 0]
    state = _queue(installed)
    assert (state["released_on_first_token"], state["released_answered"]) == (1, 0)


async def test_the_messages_passthrough_holds_the_place_through_an_empty_block_start(
    installed, monkeypatch
):
    frames = [_sse(_MESSAGE_START), _sse(_EMPTY_TEXT_START), _sse(_TEXT_DELTA), _sse(_MESSAGE_STOP)]

    pending = await _stream_passthrough(monkeypatch, installed, frames)

    # An engine may open a block before it has generated anything.
    assert pending == [1, 1, 0, 0]


async def test_the_messages_passthrough_hears_a_first_output_the_stream_left_unterminated(
    installed, monkeypatch
):
    frames = [_sse(_MESSAGE_START), _sse(_TEXT_DELTA, terminated=False)]

    pending = await _stream_passthrough(monkeypatch, installed, frames)

    # The text frame only completes at the end of the body.
    assert pending == [1, 1]
    state = _queue(installed)
    assert (state["pending"], state["released_on_first_token"], state["released_answered"]) == (
        0,
        1,
        0,
    )


def _block_start(block: dict) -> dict:
    return {"type": "content_block_start", "index": 0, "content_block": block}


def _block_delta(delta: dict) -> dict:
    return {"type": "content_block_delta", "index": 0, "delta": delta}


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (_MESSAGE_START, False),
        (_EMPTY_TEXT_START, False),
        (_block_start({"type": "thinking", "thinking": ""}), False),
        (_block_start({"type": "text", "text": "Hi"}), True),
        (_block_start({"type": "thinking", "thinking": "Let"}), True),
        (_block_start({"type": "tool_use", "id": "t", "name": "f"}), True),
        (_TEXT_DELTA, True),
        (_block_delta({"type": "text_delta", "text": ""}), False),
        (_block_delta({"type": "thinking_delta", "thinking": "x"}), True),
        (_block_delta({"type": "input_json_delta", "partial_json": ""}), False),
        (_block_delta({"type": "input_json_delta", "partial_json": "{"}), True),
        (_block_delta({"type": "signature_delta", "signature": "s"}), False),
        (_MESSAGE_STOP, False),
    ],
)
def test_the_passthrough_counts_only_generated_output_as_a_first_token(event, expected):
    from serving.adapters.anthropic import _carries_output
    from serving.servers.sse import SSEParser

    (message,) = SSEParser().feed(_sse(event))

    assert _carries_output(message) is expected


async def test_a_non_streaming_request_holds_its_place_until_the_response(installed, monkeypatch):
    adapter = _local_adapter()
    seen: list[dict[str, int]] = []

    async def fake_json_post(*_args, **_kwargs):
        seen.append(_line(installed))
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "local-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(adapter.http, "json_post_with_retry", fake_json_post)

    await adapter.chat_completion([{"role": "user", "content": "hi"}])

    assert seen == [{"pending": 1, "waiting": 0}]
    state = _queue(installed)
    assert (state["pending"], state["released_answered"]) == (0, 1)
