"""Verify that streaming responses are not buffered by the middleware stack."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import Response, StreamingResponse
from httpx import ASGITransport, AsyncClient

from serving.servers.middleware.error import FallbackErrorMiddleware
from serving.servers.middleware.request_id import RequestIdMiddleware
from serving.servers.middleware.request_log import RequestLogMiddleware
from serving.servers.middleware.timeout import TimeoutMiddleware

_CHUNK_COUNT = 5
_CHUNK_DELAY_S = 0.02


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TimeoutMiddleware)
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(FallbackErrorMiddleware)
    app.add_middleware(RequestIdMiddleware)

    @app.get("/stream")
    async def stream():
        async def generate():
            for i in range(_CHUNK_COUNT):
                yield f"data: chunk-{i}\n\n"
                await asyncio.sleep(_CHUNK_DELAY_S)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/sets-request-id")
    async def sets_request_id():
        return Response(headers={"X-Request-ID": "inner-response-id"})

    @app.get("/non-stream")
    async def non_stream():
        return {"ok": True}

    return app


async def _collect_asgi_events(app: FastAPI, path: str) -> list[tuple[float, dict[str, Any]]]:
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
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        events.append((time.perf_counter(), message))

    await app(scope, receive, send)
    return events


@pytest.mark.asyncio
async def test_streaming_chunks_arrive_incrementally():
    """Assert streaming body events are forwarded as they are produced."""
    app = _build_app()
    events = await _collect_asgi_events(app, "/stream")
    body_events = [
        (received_at, message["body"].decode())
        for received_at, message in events
        if message["type"] == "http.response.body" and message.get("body")
    ]

    chunks = [body for _, body in body_events]
    assert len(chunks) == _CHUNK_COUNT
    for i, chunk in enumerate(chunks):
        assert chunk == f"data: chunk-{i}\n\n"

    elapsed_between_first_and_last = body_events[-1][0] - body_events[0][0]
    assert elapsed_between_first_and_last >= _CHUNK_DELAY_S * (_CHUNK_COUNT - 2)


@pytest.mark.asyncio
async def test_streaming_response_has_request_id_header():
    """Assert streaming responses receive the request ID header."""
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:  # noqa: SIM117
        async with client.stream("GET", "/stream") as response:
            assert response.status_code == 200
            assert "x-request-id" in response.headers
            request_id = response.headers["x-request-id"]
            assert len(request_id) == 24
            async for _ in response.aiter_lines():
                pass


@pytest.mark.asyncio
async def test_non_streaming_response_has_request_id_header():
    """Assert non-streaming responses receive the request ID header."""
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/non-stream")
        assert response.status_code == 200
        assert "x-request-id" in response.headers


@pytest.mark.asyncio
async def test_request_id_is_unique_per_request():
    """Assert generated request IDs are unique across requests."""
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r1 = await client.get("/non-stream")
        r2 = await client.get("/non-stream")
        id1 = r1.headers["x-request-id"]
        id2 = r2.headers["x-request-id"]
        assert id1 != id2


@pytest.mark.asyncio
async def test_request_id_replaces_existing_response_header():
    """Assert request ID middleware overwrites existing response IDs."""
    app = _build_app()
    events = await _collect_asgi_events(app, "/sets-request-id")
    response_start = next(
        message for _, message in events if message["type"] == "http.response.start"
    )
    request_id_headers = [
        value for name, value in response_start["headers"] if name.lower() == b"x-request-id"
    ]

    assert len(request_id_headers) == 1
    assert request_id_headers[0] != b"inner-response-id"
