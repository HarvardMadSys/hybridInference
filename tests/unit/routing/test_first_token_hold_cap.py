"""FixedRouter's first-token wait holds a bounded amount before it releases.

While an attempt could still be offloaded, ``_await_first_token`` holds what the
stream sends before its first content, because once a frame reaches the client
the stream is committed. Nothing bounded that hold: an upstream streaming empty
deltas or keep-alives had every byte buffered until content came or the wait
ran out. It now stops at the hedge race's bound
(``routing.streaming.PRE_CONTENT_BUFFER_MAX_BYTES``): it releases what it holds,
which commits the stream to that attempt, and the wait ends.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

import pytest

from routing import routers as routers_module, streaming
from routing.offload import OffloadPolicy
from routing.routers import FixedRouter
from routing.routewise import hedging
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.adapters.upstream_limiter import (
    UpstreamConcurrencyLimiter,
    reset_upstream_limiter,
    upstream_slot,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

MODEL = "m"
MESSAGES = [{"role": "user", "content": "hi"}]
_CAP = 4096


def _chunk(delta: dict[str, Any], **extra: Any) -> str:
    payload = {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta}]}
    payload.update(extra)
    return f"data: {json.dumps(payload)}\n\n"


class _ChattyEngine(BaseAdapter):
    """Streams ``prelude`` frames with no content, then content once ``gate`` is set."""

    def __init__(self, provider: str, *, prelude: list[str], route_id: str | None = None):
        super().__init__(
            ModelConfig(
                id=MODEL,
                name=MODEL,
                provider=provider,
                base_url="http://localhost:8000/v1",
                endpoint_id=f"{MODEL}:{provider}-api",
                route_metadata={"route_id": route_id} if route_id else {},
            )
        )
        self.prelude = prelude
        self.gate = asyncio.Event()
        self.sent_prelude = asyncio.Event()
        self.calls = 0

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        raise AssertionError("streaming only")

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        self.calls += 1
        async with upstream_slot(self.config.provider, "k", base_url=self.config.base_url):
            for frame in self.prelude:
                yield frame
            self.sent_prelude.set()
            await self.gate.wait()
            yield _chunk({"content": self.config.provider})


class _Policies:
    def __init__(self, policy: OffloadPolicy) -> None:
        self.policy = policy

    def get_offload_policy(self, model_id: str) -> OffloadPolicy | None:
        return self.policy


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    reset_upstream_limiter(
        UpstreamConcurrencyLimiter(initial_limit=8, max_limit=8, acquire_timeout=10.0)
    )
    monkeypatch.setattr("random.random", lambda: 0.0)
    monkeypatch.setattr(routers_module, "PRE_CONTENT_BUFFER_MAX_BYTES", _CAP)
    yield
    reset_upstream_limiter()


def _router(engine: BaseAdapter, reserved: BaseAdapter, *, wait_seconds: float) -> FixedRouter:
    router = FixedRouter(
        offload_policy_resolver=_Policies(
            OffloadPolicy(route_id="offload", wait_seconds=wait_seconds)
        )
    )
    router.register_route(MODEL, [(engine, 1.0), (reserved, 1.0)])
    return router


def _padded_prelude(frames: int) -> list[str]:
    # Role deltas carrying ~1 KiB each and no content: what a chatty engine
    # might send before it starts answering.
    return [_chunk({"role": "assistant"}, padding="x" * 1024) for _ in range(frames)]


def _text(chunks: list[str]) -> str:
    out = ""
    for chunk in chunks:
        payload = json.loads(chunk.removeprefix("data: ").strip())
        for choice in payload.get("choices") or []:
            out += (choice.get("delta") or {}).get("content") or ""
    return out


@pytest.mark.unit
def test_the_hedge_race_and_the_first_token_wait_share_one_bound():
    assert hedging._STREAM_RACE_BUFFER_MAX_BYTES == streaming.PRE_CONTENT_BUFFER_MAX_BYTES


@pytest.mark.unit
@pytest.mark.asyncio
async def test_holding_stops_at_the_cap_and_commits_the_stream(caplog):
    engine = _ChattyEngine("engine", prelude=_padded_prelude(8))
    reserved = _ChattyEngine("reserved", prelude=[], route_id="offload")
    reserved.gate.set()
    router = _router(engine, reserved, wait_seconds=0.05)
    received: list[str] = []

    async def consume() -> None:
        async for chunk in router.stream_chat_completion(MODEL, MESSAGES):
            received.append(chunk)

    with caplog.at_level(logging.WARNING, logger="routing.routers"):
        task = asyncio.create_task(consume())
        await asyncio.wait_for(engine.sent_prelude.wait(), timeout=5)
        await asyncio.sleep(0)
        # Released before any content: the routing frame, then what was held.
        held_out = [chunk for chunk in received if '"padding"' in chunk]
        assert len(held_out) >= _CAP // 1024
        # Well past the 50 ms wait: had it still been running, the request would
        # have gone to the offload route by now.
        await asyncio.sleep(0.15)
        engine.gate.set()
        await asyncio.wait_for(task, timeout=5)

    assert _text(received) == "engine"
    assert reserved.calls == 0
    (record,) = [
        r for r in caplog.records if getattr(r, "event", None) == "first_token_hold_cap_reached"
    ]
    assert record.endpoint_id == "m:engine-api"
    assert record.held_bytes >= _CAP
    # The first token, when it came, was still recorded: the lease is back.
    assert router.prefill_load.backlog("m:engine-api") == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_below_the_cap_the_wait_is_unchanged():
    """A short prelude is held and the stream still goes to the offload route."""
    engine = _ChattyEngine("engine", prelude=_padded_prelude(2))
    reserved = _ChattyEngine("reserved", prelude=[], route_id="offload")
    reserved.gate.set()
    router = _router(engine, reserved, wait_seconds=0.05)

    chunks = [chunk async for chunk in router.stream_chat_completion(MODEL, MESSAGES)]

    assert _text(chunks) == "reserved"
    # The engine's prelude never reached the client.
    assert not [chunk for chunk in chunks if '"padding"' in chunk]
