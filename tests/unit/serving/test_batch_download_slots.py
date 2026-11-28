"""Rendered batch downloads take one of a process's few slots (M27).

A download renders its batch's rows on the event loop and holds a page of up
to ``PAGE_BYTES`` while its client reads, however slowly, and nothing limited
how many ran at once. Now each takes a slot, per owner and per process, given
back however the download ends, even when its response is never run; and each
response or request is released as soon as its line is rendered, and sent
about every ``_SEND_BYTES``, rather than kept until the page ends.
"""

from __future__ import annotations

import gc
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from serving.batch import delivery
from tests.unit.serving.test_batch_delivery_pages import PagedStore


async def _body(lines: int = 2):
    for n in range(lines):
        yield f"{n}\n".encode()


@pytest.fixture(autouse=True)
def _no_downloads(monkeypatch):
    monkeypatch.setattr(delivery, "_downloads", delivery.Counter())


async def test_an_owner_and_a_process_have_only_so_many_downloads_at_once(monkeypatch):
    monkeypatch.setattr(delivery, "MAX_DOWNLOADS", 3)
    monkeypatch.setattr(delivery, "MAX_DOWNLOADS_PER_OWNER", 2)
    alice = [delivery.hold_download(_body(), "alice") for _ in range(2)]
    assert all(held is not None for held in alice)
    assert delivery.hold_download(_body(), "alice") is None
    bob = delivery.hold_download(_body(), "bob")
    assert bob is not None
    # The process is full: someone else is refused too.
    assert delivery.hold_download(_body(), "carol") is None

    await alice[0].aclose()
    assert delivery.hold_download(_body(), "carol") is not None


async def test_a_slot_is_given_back_once_however_the_download_ends():
    finished = delivery.hold_download(_body(), "alice")
    assert [part async for part in finished] == [b"0\n", b"1\n"]
    await finished.aclose()

    async def failing():
        yield b"0\n"
        raise delivery.DownloadInterrupted("b")

    broken = delivery.hold_download(failing(), "alice")
    with pytest.raises(delivery.DownloadInterrupted):
        [part async for part in broken]

    # Closed before its first line, as a response does when its client left.
    unread = delivery.hold_download(_body(), "alice")
    await unread.aclose()
    await unread.aclose()

    assert dict(delivery._downloads) == {}


async def test_each_response_is_released_once_its_line_is_rendered():
    store = PagedStore(["completed"] * 3)
    seen: list[dict[str, Any]] = []
    result_page = store.result_page

    async def remembered(*args: Any, **kwargs: Any):
        page, more = await result_page(*args, **kwargs)
        seen.extend(page)
        return page, more

    store.result_page = remembered
    stream = delivery.stream_lines(store=store, batch_id="b", kind="all")
    await stream.__anext__()
    # The page is still held while the client reads; its responses are not.
    assert len(seen) == 3
    assert not any("response" in row or "response_text" in row for row in seen)
    await stream.aclose()


async def test_a_slot_comes_back_when_a_body_never_run_is_dropped():
    """A response built but never run neither reads nor closes its body."""
    held = delivery.hold_download(_body(), "alice")
    assert dict(delivery._downloads) == {"alice": 1}

    del held
    gc.collect()

    assert dict(delivery._downloads) == {}


def _stored_requests(count: int) -> list[dict[str, Any]]:
    """Rows as ``request_page`` returns them: each request still its stored text."""
    rows = []
    for n in range(1, count + 1):
        text = json.dumps({"model": "m", "input": f"q{n} ✓"}, ensure_ascii=False)
        rows.append(
            {"id": n, "custom_id": f"req-{n}", "request_text": text, "status": "failed", "bytes": 9}
        )
    return rows


async def test_a_request_export_sends_its_page_in_parts(monkeypatch):
    """A page of stored requests went out in one piece, rendered whole before it was sent."""
    monkeypatch.setattr(delivery, "_SEND_BYTES", 1)
    rows = _stored_requests(3)
    store = AsyncMock()
    store.take_read_lease.return_value = "lease"
    store.batch_intact.return_value = True
    store.request_page.return_value = (rows, False)

    stream = delivery.stream_requests(store=store, batch_id="b", url="/v1/embeddings")
    first = await anext(stream)
    # The client has the first line; the rest of the page is not rendered yet,
    # and what was sent is no longer held.
    assert "request" not in rows[0] and "request_text" not in rows[0]
    assert "request_text" in rows[1]
    parts = [first] + [part async for part in stream]

    assert len(parts) == 3
    assert b"".join(parts) == b"".join(
        json.dumps(
            {
                "custom_id": f"req-{n}",
                "method": "POST",
                "url": "/v1/embeddings",
                "body": {"model": "m", "input": f"q{n} ✓"},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        + b"\n"
        for n in (1, 2, 3)
    )
