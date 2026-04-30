"""Tests for the request timeout middleware."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.middleware.timeout import TimeoutMiddleware


def _build_app(timeout_s: float) -> FastAPI:
    app = FastAPI()
    app.add_middleware(TimeoutMiddleware, timeout_s=timeout_s)

    @app.get("/fast")
    async def fast() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/slow")
    async def slow() -> dict[str, str]:
        await asyncio.sleep(1.0)
        return {"ok": "yes"}

    return app


@pytest.mark.asyncio
async def test_fast_request_passes_through() -> None:
    app = _build_app(timeout_s=1.0)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/fast")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_slow_request_returns_504() -> None:
    app = _build_app(timeout_s=0.05)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slow")
    assert response.status_code == 504
    assert "Gateway Timeout" in response.text
