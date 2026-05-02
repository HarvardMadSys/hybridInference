"""Tests for the request timeout middleware."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.middleware.timeout import (
    _DEFAULT_TIMEOUT_S,
    TimeoutMiddleware,
    _parse_timeout_env,
)


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


def test_parse_timeout_env_uses_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REQUEST_TIMEOUT_SECONDS", raising=False)
    assert _parse_timeout_env() == _DEFAULT_TIMEOUT_S


def test_parse_timeout_env_uses_default_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "  ")
    assert _parse_timeout_env() == _DEFAULT_TIMEOUT_S


def test_parse_timeout_env_uses_default_when_non_numeric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "not-a-number")
    assert _parse_timeout_env() == _DEFAULT_TIMEOUT_S


def test_parse_timeout_env_uses_default_when_non_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "0")
    assert _parse_timeout_env() == _DEFAULT_TIMEOUT_S
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "-5")
    assert _parse_timeout_env() == _DEFAULT_TIMEOUT_S


def test_parse_timeout_env_parses_valid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "30.5")
    assert _parse_timeout_env() == 30.5
