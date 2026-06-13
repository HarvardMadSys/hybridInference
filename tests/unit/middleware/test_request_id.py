"""Unit tests for RequestIdMiddleware request-context seeding."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.middleware.request_id import RequestIdMiddleware
from serving.utils import context as req_ctx


def _app_capturing_context(captured: dict) -> FastAPI:
    app = FastAPI()

    @app.get("/x")
    async def _handler() -> dict:
        captured.update(req_ctx.get())
        return {"ok": True}

    app.add_middleware(RequestIdMiddleware)
    return app


@pytest.mark.asyncio
async def test_captures_user_agent_into_context() -> None:
    captured: dict = {}
    transport = ASGITransport(app=_app_capturing_context(captured))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        await client.get("/x", headers={"user-agent": "my-client/9.9"})
    assert captured.get("client_user_agent") == "my-client/9.9"
    assert captured.get("request_id")


@pytest.mark.asyncio
async def test_blank_user_agent_omits_key() -> None:
    captured: dict = {}
    transport = ASGITransport(app=_app_capturing_context(captured))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        await client.get("/x", headers={"user-agent": ""})
    assert "client_user_agent" not in captured
