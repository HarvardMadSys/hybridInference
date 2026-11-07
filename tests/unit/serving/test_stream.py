from __future__ import annotations

import asyncio
import json

import pytest

from serving.stream import (
    STREAM_QUEUE_MAX_BYTES,
    STREAM_QUEUE_MAX_ITEMS,
    StreamQueue,
    make_final_usage_chunk,
    make_stream_chunk,
    pump_stream,
)


@pytest.mark.unit
def test_make_stream_chunk_structure():
    line = make_stream_chunk(model="m", content="hi")
    assert line.startswith("data: ")
    payload = json.loads(line[len("data: ") :])
    assert payload["object"] == "chat.completion.chunk"
    assert payload["model"] == "m"
    assert payload["choices"][0]["delta"]["content"] == "hi"


@pytest.mark.unit
def test_make_final_usage_chunk_estimates_tokens():
    messages = [{"role": "user", "content": "hello"}]
    # total_content with 2 words should produce non-zero completion tokens
    line = make_final_usage_chunk(model="m", messages=messages, total_content="ok then")
    payload = json.loads(line[len("data: ") :])
    usage = payload["usage"]
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


@pytest.mark.unit
@pytest.mark.perf
def test_make_stream_chunk_performance():
    """Ensure generating many chunks is fast enough for regressions."""
    import time

    start = time.perf_counter()
    for _ in range(1000):
        _ = make_stream_chunk(model="m", content="content")
    elapsed = time.perf_counter() - start
    assert elapsed < 0.1  # 1000 chunks within 100ms on CI-class machines


# -- the reader a streaming response consumes its upstream through ---------------

_END = object()


class _Upstream:
    """An upstream that counts what it produced and whether it was closed."""

    def __init__(self, chunks: int, size: int = 8, fail_after: int | None = None) -> None:
        self.chunks, self.size, self.fail_after = chunks, size, fail_after
        self.produced = 0
        self.closed = False

    async def stream(self):
        try:
            for index in range(self.chunks):
                if self.fail_after is not None and index == self.fail_after:
                    raise RuntimeError("upstream reset")
                self.produced += 1
                yield f"{index:0{self.size}d}"
                await asyncio.sleep(0)
        finally:
            self.closed = True


async def _let_it_run() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_reader_stops_reading_while_its_response_takes_nothing():
    upstream = _Upstream(10_000)
    queue = StreamQueue()
    task = asyncio.create_task(pump_stream(upstream.stream(), queue, end=_END))
    try:
        await queue.get()  # the client read one chunk, then stopped reading
        await _let_it_run()
        assert upstream.produced <= STREAM_QUEUE_MAX_ITEMS + 2

        received = 1
        while await queue.get() is not _END:
            received += 1
        assert received == 10_000
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_large_chunks_are_held_back_by_bytes():
    upstream = _Upstream(1_000, size=64 * 1024)
    queue = StreamQueue()
    task = asyncio.create_task(pump_stream(upstream.stream(), queue, end=_END))
    try:
        await _let_it_run()
        # About a mebibyte queued, not the whole 64 MiB generation.
        assert upstream.produced * 64 * 1024 <= STREAM_QUEUE_MAX_BYTES + 2 * 64 * 1024
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_a_reader_waiting_on_a_full_queue_is_torn_down_and_closes_its_upstream():
    upstream = _Upstream(10_000)
    queue = StreamQueue(max_items=4)
    task = asyncio.create_task(pump_stream(upstream.stream(), queue, end=_END))
    await _let_it_run()
    assert queue.full()  # the client is gone; the reader waits to put the next

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    # Closed at once, which ends the upstream request and gives back its slot.
    assert upstream.closed
    assert queue.qsize() == 4  # nothing more was put on the way out


@pytest.mark.asyncio
async def test_an_upstream_error_is_queued_before_the_end():
    upstream = _Upstream(10, fail_after=2)
    queue = StreamQueue()
    await pump_stream(upstream.stream(), queue, end=_END)

    items = [queue.get_nowait() for _ in range(queue.qsize())]
    assert items[:2] == ["00000000", "00000001"]
    assert isinstance(items[2], RuntimeError)
    assert items[3] is _END
    assert upstream.closed


@pytest.mark.asyncio
async def test_a_chunk_larger_than_the_byte_bound_still_goes_in():
    queue = StreamQueue(max_bytes=10)
    await asyncio.wait_for(queue.put("x" * 100), timeout=1)
    assert queue.full()
    assert await queue.get() == "x" * 100
    assert not queue.full()


@pytest.mark.asyncio
async def test_a_cancellation_the_upstream_raises_itself_still_ends_the_stream():
    """Nobody cancelled the reader: the response must not wait for a chunk forever."""

    async def cut_short():
        yield "first"
        raise asyncio.CancelledError  # e.g. the connection's own teardown

    queue = StreamQueue()
    task = asyncio.create_task(pump_stream(cut_short(), queue, end=_END))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert queue.get_nowait() == "first"
    assert queue.get_nowait() is _END
