"""Settings read per request follow the configuration without a restart."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.config import app_config
from serving.config.settings import get_settings
from serving.servers.middleware.cors import SettingsCORSMiddleware
from serving.servers.middleware.timeout import TimeoutMiddleware
from tests.fixtures.app_config_store import row


def _cors_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        SettingsCORSMiddleware,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.mark.asyncio
async def test_cors_origins_follow_the_setting(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://console.example")
    get_settings.cache_clear()
    transport = ASGITransport(app=_cors_app())

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        allowed = await client.get("/ping", headers={"Origin": "https://console.example"})
        other = await client.get("/ping", headers={"Origin": "https://new.example"})
        assert allowed.headers["access-control-allow-origin"] == "https://console.example"
        assert allowed.headers["access-control-allow-credentials"] == "true"
        assert "access-control-allow-origin" not in other.headers

        app_config._apply(
            {"CORS_ALLOWED_ORIGINS": row("CORS_ALLOWED_ORIGINS", "https://new.example")},
            boot=False,
        )

        now_allowed = await client.get("/ping", headers={"Origin": "https://new.example"})
        now_other = await client.get("/ping", headers={"Origin": "https://console.example"})
        preflight = await client.options(
            "/ping",
            headers={
                "Origin": "https://new.example",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert now_allowed.headers["access-control-allow-origin"] == "https://new.example"
    assert "access-control-allow-origin" not in now_other.headers
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "https://new.example"
    assert preflight.headers["access-control-allow-credentials"] == "true"


@pytest.mark.asyncio
async def test_request_timeout_is_read_for_every_request(monkeypatch) -> None:
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "5")
    app = FastAPI()
    app.add_middleware(TimeoutMiddleware)

    @app.get("/slow")
    async def slow() -> dict[str, bool]:
        await asyncio.sleep(0.3)
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/slow")).status_code == 200

        monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "0.05")

        assert (await client.get("/slow")).status_code == 504


def test_the_request_timeouts_follow_stored_values(monkeypatch) -> None:
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "5")
    monkeypatch.delenv("STREAM_REQUEST_TIMEOUT_SECONDS", raising=False)
    middleware = TimeoutMiddleware(app=None)  # type: ignore[arg-type]
    assert middleware._timeout_s == 5.0
    assert middleware._stream_timeout_s == 3600.0

    app_config._apply(
        {
            "REQUEST_TIMEOUT_SECONDS": row("REQUEST_TIMEOUT_SECONDS", "30"),
            "STREAM_REQUEST_TIMEOUT_SECONDS": row("STREAM_REQUEST_TIMEOUT_SECONDS", "0"),
        },
        boot=False,
    )

    assert middleware._timeout_s == 30.0
    assert middleware._stream_timeout_s is None  # 0 removes the stream cap


def test_admin_provider_base_urls_follow_the_setting(monkeypatch) -> None:
    from serving.servers.routers.admin.provider_routes import PROVIDER_TARGETS

    monkeypatch.delenv("CHUTES_BASE_URL", raising=False)
    assert PROVIDER_TARGETS["chutes"].default_base_url == "https://llm.chutes.ai/v1"

    app_config._apply(
        {"CHUTES_BASE_URL": row("CHUTES_BASE_URL", "https://chutes.internal/v1")}, boot=False
    )

    assert PROVIDER_TARGETS["chutes"].default_base_url == "https://chutes.internal/v1"
    assert PROVIDER_TARGETS.get("chutes").default_base_url == "https://chutes.internal/v1"
    assert "chutes" in PROVIDER_TARGETS
    assert {target.provider for target in PROVIDER_TARGETS.values()} >= {"chutes", "openrouter"}


def test_provider_keys_resolve_through_the_database(monkeypatch) -> None:
    from serving.adapters import dynamic_keys

    monkeypatch.setenv("MINIMAX_API_KEY", "env-key")
    monkeypatch.delenv("MINIMAX_API_KEY2", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY3", raising=False)

    app_config._apply(
        {
            "MINIMAX_API_KEY": row("MINIMAX_API_KEY", "db-key", secret=True),
            "MINIMAX_API_KEY2": row("MINIMAX_API_KEY2", "db-key-2", secret=True),
        },
        boot=True,
    )

    assert dynamic_keys.configured_env_keys_for_provider("minimax") == ["db-key", "db-key-2"]
