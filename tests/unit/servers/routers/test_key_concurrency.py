"""Tests for the concurrency cap reported by GET /v1/key."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.servers.auth import verify_api_key_for_balance
from serving.servers.deps import AppServices
from serving.servers.routers import models


class _RuntimeSettings:
    def __init__(self, cap: int) -> None:
        self.cap = cap

    async def get_int(self, key: str) -> int:
        return self.cap


def _app(user_ctx: dict, *, limiter: object | None, cap: int = 3) -> FastAPI:
    app = FastAPI()
    app.state.services = AppServices(
        router=FixedRouter(),
        user_concurrency_limiter=limiter,
        runtime_settings=_RuntimeSettings(cap),
    )
    app.dependency_overrides[verify_api_key_for_balance] = lambda: user_ctx
    app.include_router(models.router)
    return app


async def _get(app: FastAPI, headers: dict[str, str] | None = None) -> dict:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/key", headers=headers)
    assert response.status_code == 200
    return response.json()


@pytest.mark.asyncio
async def test_reports_the_role_cap():
    app = _app({"user_id": "u1", "role": "free", "authenticated": True}, limiter=object())

    body = await _get(app)

    assert body["concurrency"] == {"max_in_flight": 3, "project_max_in_flight": None}


@pytest.mark.asyncio
async def test_reports_an_account_override():
    app = _app(
        {"user_id": "u1", "role": "free", "authenticated": True, "max_concurrent_requests": 8},
        limiter=object(),
    )

    body = await _get(app)

    assert body["concurrency"] == {"max_in_flight": 8, "project_max_in_flight": None}


@pytest.mark.asyncio
async def test_unlimited_is_reported_as_null():
    app = _app(
        {"user_id": "u1", "role": "admin", "authenticated": True},
        limiter=object(),
        cap=0,
    )

    body = await _get(app)

    assert body["concurrency"] == {"max_in_flight": None, "project_max_in_flight": None}


@pytest.mark.asyncio
async def test_no_limiter_means_inference_is_unguarded():
    app = _app({"user_id": "u1", "role": "free", "authenticated": True}, limiter=None)

    body = await _get(app)

    assert body["concurrency"] == {"max_in_flight": None, "project_max_in_flight": None}


@pytest.mark.asyncio
async def test_on_behalf_of_reports_the_target_cap(monkeypatch):
    target = {"user_id": "u2", "role": "pro", "max_concurrent_requests": 5}
    resolve = AsyncMock(return_value=target)
    monkeypatch.setattr(models, "_resolve_effective_identity", resolve)
    app = _app(
        {"user_id": "service", "role": "free", "authenticated": True, "auth_key_hash": "h"},
        limiter=object(),
    )

    body = await _get(app, headers={"X-On-Behalf-Of": "u2"})

    assert body["concurrency"] == {"max_in_flight": 5, "project_max_in_flight": None}
    resolve.assert_awaited_once()


@pytest.mark.asyncio
async def test_reports_the_project_shared_cap():
    app = _app(
        {
            "user_id": "u1",
            "role": "free",
            "authenticated": True,
            "project_id": "proj-1",
            "project_max_concurrent_requests": 2,
        },
        limiter=object(),
    )

    body = await _get(app)

    assert body["concurrency"] == {"max_in_flight": 3, "project_max_in_flight": 2}


@pytest.mark.asyncio
async def test_an_unlimited_project_cap_is_reported_as_null():
    app = _app(
        {
            "user_id": "u1",
            "role": "free",
            "authenticated": True,
            "project_id": "proj-1",
            "project_max_concurrent_requests": 0,
        },
        limiter=object(),
    )

    body = await _get(app)

    assert body["concurrency"] == {"max_in_flight": 3, "project_max_in_flight": None}
