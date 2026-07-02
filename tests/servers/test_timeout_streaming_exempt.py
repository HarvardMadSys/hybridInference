"""Regression tests: TimeoutMiddleware must not cancel healthy SSE streams.

The total request timeout used to wrap the entire ASGI call including the
StreamingResponse body iteration, so long-but-healthy event streams were killed
mid-body at the total cap. These tests drive the middleware directly with stub
ASGI apps and a tiny timeout to lock the behaviour in.
"""

from __future__ import annotations

import anyio

from apps.backend.serving.servers.middleware.timeout import TimeoutMiddleware


def _make_scope() -> dict:
    return {"type": "http", "method": "GET", "path": "/"}


async def _noop_receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


async def test_sse_stream_survives_total_timeout() -> None:
    """A slow event-stream body is delivered and never gets a 504 injected."""

    async def sse_app(scope, receive, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        # Simulate a long, healthy stream that exceeds the total timeout.
        await anyio.sleep(0.2)
        await send({"type": "http.response.body", "body": b"data: hi\n\n"})

    sent: list[dict] = []

    async def capture(message: dict) -> None:
        sent.append(message)

    mw = TimeoutMiddleware(sse_app, timeout_s=0.05)
    await mw(_make_scope(), _noop_receive, capture)

    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) == 1
    assert starts[0]["status"] == 200
    assert not any(m.get("status") == 504 for m in starts)
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert any(m.get("body") == b"data: hi\n\n" for m in bodies)


async def test_non_sse_slow_request_returns_504() -> None:
    """A slow non-streaming request still gets cancelled and yields a 504."""

    async def slow_app(scope, receive, send) -> None:
        await anyio.sleep(0.2)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    sent: list[dict] = []

    async def capture(message: dict) -> None:
        sent.append(message)

    mw = TimeoutMiddleware(slow_app, timeout_s=0.05)
    await mw(_make_scope(), _noop_receive, capture)

    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) == 1
    assert starts[0]["status"] == 504
