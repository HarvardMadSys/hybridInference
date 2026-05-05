"""Verify that streaming responses are not buffered by the middleware stack."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

from serving.servers.middleware.error import FallbackErrorMiddleware
from serving.servers.middleware.request_id import RequestIdMiddleware
from serving.servers.middleware.request_log import RequestLogMiddleware
from serving.servers.middleware.timeout import TimeoutMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(FallbackErrorMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(TimeoutMiddleware)
    app.add_middleware(RequestLogMiddleware)

    @app.get("/stream")
    async def stream():
        async def generate():
            for i in range(5):
                yield f"data: chunk-{i}\n\n"
                await asyncio.sleep(0.05)

        return StreamingResponse(generate(), media_type="text/event-stream")

    return app


@pytest.mark.asyncio
async def test_streaming_chunks_arrive_incrementally():
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:  # noqa: SIM117
        async with client.stream("GET", "/stream") as response:
            assert response.status_code == 200
            chunks: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith("data: chunk-"):
                    chunks.append(line)

    assert len(chunks) == 5
    for i, chunk in enumerate(chunks):
        assert chunk == f"data: chunk-{i}"


@pytest.mark.asyncio
async def test_streaming_response_has_request_id_header():
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
