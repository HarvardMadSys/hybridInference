"""The gateway's ``X-Content-Type-Options: nosniff`` on every response, streams included."""

from __future__ import annotations

import asyncio
import time
from itertools import pairwise
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import Response, StreamingResponse
from httpx import ASGITransport, AsyncClient

from serving.servers.middleware.body_limit import RequestBodyLimitMiddleware
from serving.servers.middleware.error import FallbackErrorMiddleware
from serving.servers.middleware.request_id import RequestIdMiddleware
from serving.servers.middleware.request_log import RequestLogMiddleware
from serving.servers.middleware.security_headers import NoSniffMiddleware
from serving.servers.middleware.timeout import TimeoutMiddleware

_FRAMES = 5
_FRAME_DELAY_S = 0.05


def _build_app() -> FastAPI:
    """The gateway's middleware order, outermost last, around three routes."""
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(TimeoutMiddleware)
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(FallbackErrorMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(NoSniffMiddleware)

    @app.get("/stream")
    async def stream():
        async def generate():
            for i in range(_FRAMES):
                yield f"data: frame-{i}\n\n"
                await asyncio.sleep(_FRAME_DELAY_S)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/plain")
    async def plain():
        return {"ok": True}

    @app.get("/own-header")
    async def own_header():
        return Response(headers={"X-Content-Type-Options": "nosniff"})

    @app.get("/boom")
    async def boom():
        raise RuntimeError("unhandled")

    return app


async def _asgi_events(app: Any, path: str) -> list[tuple[float, dict[str, Any]]]:
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "server": ("test", 80),
        "client": ("testclient", 50000),
        "root_path": "",
    }
    events: list[tuple[float, dict[str, Any]]] = []
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        events.append((time.perf_counter(), message))

    await app(scope, receive, send)
    return events


def _nosniff_values(start: dict[str, Any]) -> list[bytes]:
    return [value for name, value in start["headers"] if name.lower() == b"x-content-type-options"]


@pytest.mark.asyncio
async def test_a_stream_arrives_frame_by_frame_with_the_header() -> None:
    """The header is on the response start, and the body still streams."""
    events = await _asgi_events(_build_app(), "/stream")

    start = next(message for _, message in events if message["type"] == "http.response.start")
    assert _nosniff_values(start) == [b"nosniff"]
    bodies = [
        (at, message["body"].decode())
        for at, message in events
        if message["type"] == "http.response.body" and message.get("body")
    ]
    assert [body for _, body in bodies] == [f"data: frame-{i}\n\n" for i in range(_FRAMES)]
    # Each frame left as it was produced, not all at the end: the gaps between
    # them add up to the generator's own sleeps.
    gaps = [later - earlier for (earlier, _), (later, _) in pairwise(bodies)]
    assert min(gaps) >= _FRAME_DELAY_S * 0.5
    assert bodies[-1][0] - bodies[0][0] >= _FRAME_DELAY_S * (_FRAMES - 1) * 0.8


@pytest.mark.asyncio
async def test_a_plain_response_gets_the_header() -> None:
    async with AsyncClient(transport=ASGITransport(app=_build_app()), base_url="http://t") as c:
        response = await c.get("/plain")
    assert response.status_code == 200
    assert response.headers.get_list("x-content-type-options") == ["nosniff"]


@pytest.mark.asyncio
async def test_a_response_that_sets_the_header_keeps_one() -> None:
    events = await _asgi_events(_build_app(), "/own-header")
    start = next(message for _, message in events if message["type"] == "http.response.start")
    assert _nosniff_values(start) == [b"nosniff"]


@pytest.mark.asyncio
async def test_an_error_answer_gets_the_header() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_build_app(), raise_app_exceptions=False), base_url="http://t"
    ) as c:
        response = await c.get("/boom")
    assert response.status_code == 500
    assert response.headers.get_list("x-content-type-options") == ["nosniff"]


@pytest.mark.asyncio
async def test_other_scopes_pass_through_untouched() -> None:
    seen: list[str] = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])
        await send({"type": "lifespan.startup.complete"})

    sent: list[dict[str, Any]] = []

    async def send(message):
        sent.append(message)

    await NoSniffMiddleware(inner)({"type": "lifespan"}, None, send)
    assert seen == ["lifespan"]
    assert sent == [{"type": "lifespan.startup.complete"}]


def test_the_gateway_registers_it_outermost() -> None:
    from serving.servers.app import create_app

    middleware = [entry.cls for entry in create_app().user_middleware]
    # user_middleware lists the outermost first.
    assert middleware[0] is NoSniffMiddleware
