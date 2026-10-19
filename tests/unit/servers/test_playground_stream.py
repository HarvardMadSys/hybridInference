"""The playground stream ends every request with bytes the browser can act on.

The console reaches the playground through the Next.js rewrite proxy, which
never tells the browser about an upstream that dropped mid-response and drops
one that stays silent for 30s. So a failure must arrive as an in-band error
frame, a quiet upstream must be covered by heartbeats, and a stalled one must
end on a ceiling -- each as a stream that finishes, not a connection that
breaks.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

from routing.routers import AllCircuitsOpenError
from serving.servers.routers import playground
from serving.stream import done_sentinel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable


def _content(text: str) -> str:
    frame = {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
    return f"data: {json.dumps(frame)}\n\n"


class _Router:
    """The one surface of the router the playground uses, around a script."""

    def __init__(self, script: Callable[[], AsyncIterator[str]]) -> None:
        self.routes: dict[str, Any] = {}
        self._script = script
        self.closed = False

    def stream_chat_completion(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
        async def _run() -> AsyncIterator[str]:
            try:
                async for chunk in self._script():
                    yield chunk
            finally:
                self.closed = True

        return _run()


async def _open(router: _Router) -> AsyncIterator[str]:
    body = playground.PlaygroundChatRequest(model="m", messages=[{"role": "user", "content": "hi"}])
    response = await playground.playground_chat(body, _admin={}, router_exec=router)
    return response.body_iterator


async def _drain(router: _Router) -> list[str]:
    return [chunk async for chunk in await _open(router)]


def _error(frames: list[str]) -> dict[str, Any]:
    assert frames[-1].startswith("data: ")
    return json.loads(frames[-1][6:])["error"]


@pytest.fixture
def fast_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(playground, "_KEEPALIVE_INTERVAL", 0.02)
    monkeypatch.setattr(playground, "_MAX_IDLE_S", 0.1)


@pytest.mark.asyncio
async def test_a_failure_after_output_ends_with_an_error_frame() -> None:
    async def script() -> AsyncIterator[str]:
        yield _content("partial ")
        raise RuntimeError("upstream reset by http://10.0.0.5:8000/v1")

    router = _Router(script)
    frames = await _drain(router)

    assert '"partial "' in frames[1]
    error = _error(frames)
    assert error["code"] == 500
    assert "10.0.0.5" not in error["message"]
    assert router.closed


@pytest.mark.asyncio
async def test_a_failure_before_any_output_ends_with_an_error_frame() -> None:
    async def script() -> AsyncIterator[str]:
        raise AllCircuitsOpenError("all providers failed for m")
        yield  # pragma: no cover - makes this a generator

    frames = await _drain(_Router(script))

    assert frames[0].startswith("data: ")  # the role chunk
    assert _error(frames)["code"] == 503


@pytest.mark.asyncio
async def test_a_quiet_upstream_is_covered_by_heartbeats(fast_clock: None) -> None:
    async def script() -> AsyncIterator[str]:
        await asyncio.sleep(0.07)
        yield _content("late answer")
        yield done_sentinel()

    frames = await _drain(_Router(script))

    assert ": keepalive\n\n" in frames
    assert '"late answer"' in frames[-2]
    assert frames[-1] == done_sentinel()


@pytest.mark.asyncio
async def test_output_resets_the_idle_ceiling(fast_clock: None) -> None:
    async def script() -> AsyncIterator[str]:
        for word in ("one ", "two ", "three"):
            await asyncio.sleep(0.07)
            yield _content(word)

    frames = await _drain(_Router(script))

    assert '"three"' in frames[-1]
    assert not any('"error"' in frame for frame in frames)


@pytest.mark.asyncio
async def test_a_stalled_upstream_ends_on_the_ceiling(fast_clock: None) -> None:
    async def script() -> AsyncIterator[str]:
        yield _content("started ")
        await asyncio.sleep(60)
        yield _content("never")

    router = _Router(script)
    frames = await asyncio.wait_for(_drain(router), timeout=5)

    error = _error(frames)
    assert error["code"] == 504
    assert "sent nothing" in error["message"]
    assert router.closed


@pytest.mark.asyncio
async def test_a_client_disconnect_closes_the_upstream() -> None:
    async def script() -> AsyncIterator[str]:
        yield _content("first ")
        await asyncio.sleep(60)
        yield _content("never")

    router = _Router(script)
    stream = await _open(router)
    await anext(stream)  # role chunk
    await anext(stream)  # first content
    await stream.aclose()

    assert router.closed
