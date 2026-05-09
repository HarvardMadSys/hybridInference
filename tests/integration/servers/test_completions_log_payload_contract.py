"""Contract test: ``api_logs`` row keyset must remain stable across PRs A/B/C.

Locks in the kwargs that ``LogStore.log_request`` receives from the
chat-completions handler. Adding or removing a key here is a regression
that would break the admin/recent-requests UI, billing reports, and the
Slack alert rules from PR #372 — all of which read these rows.

Two payload shapes are pinned: the **success** path (200, with usage and
pricing) and the **error** path (5xx, with ``error`` set and pricing
absent).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import completions

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


# Authoritative kwargs the api_logs table + admin UI consume on the
# **non-streaming** success path. The streaming path additionally includes
# ``ttft_ms``; we don't pin streaming here because the contract is the same
# minus that one field — adding a key in either path is a regression.
EXPECTED_SUCCESS_LOG_KEYS = frozenset(
    {
        "request_id",
        "model_id",
        "provider",
        "prompt",
        "response",
        "usage",
        "latency_ms",
        "status_code",
        "params",
        "metadata",
        "pricing",
        "upstream_cost_usd",
    }
)


# Error-path keyset. ``pricing`` is included as ``None`` so downstream
# consumers can treat the field as always-present and don't need a default
# branch when reading the row. ``upstream_cost_usd``/``ttft_ms`` are absent
# because there's no successful upstream call to derive them from.
EXPECTED_ERROR_LOG_KEYS = frozenset(
    {
        "request_id",
        "model_id",
        "provider",
        "prompt",
        "response",
        "usage",
        "latency_ms",
        "status_code",
        "error",
        "params",
        "metadata",
        "pricing",
    }
)


class _StubAdapter(BaseAdapter):
    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        resp = self.format_response(content="ok", model=self.config.id)
        # Inject _routing exactly the way real adapters/routers do.
        resp["_routing"] = {
            "provider": self.config.provider,
            "base_url": self.config.base_url,
            "endpoint_id": getattr(self.config, "endpoint_id", None),
        }
        return resp

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        yield "data: [DONE]\n\n"


class _ErrorAdapter(BaseAdapter):
    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        raise RuntimeError("simulated upstream failure")

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        raise RuntimeError("simulated upstream failure")


def _mk_cfg(model_id: str) -> ModelConfig:
    return ModelConfig(
        id=model_id,
        name=model_id,
        provider="test",
        base_url="http://test",
        context_length=8192,
        max_output_length=4096,
        supported_params=["temperature", "top_p", "max_tokens"],
    )


def _build_app(adapter: BaseAdapter) -> tuple[FastAPI, MagicMock]:
    """Build a minimal FastAPI app with the completions router and a mock log_store."""
    router = RouteExecutor()
    router.register_route("gpt-4", [(adapter, 1.0)])

    log_store = MagicMock()
    log_store.log_request = AsyncMock()

    app = FastAPI(title="Contract Test")
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        log_store=log_store,
    )
    install_error_handlers(app)
    app.include_router(completions.router)
    return app, log_store


@pytest.mark.asyncio
async def test_log_payload_success_keyset(monkeypatch):
    """Success path: kwargs to log_request must equal EXPECTED_SUCCESS_LOG_KEYS."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")

    app, log_store = _build_app(_StubAdapter(_mk_cfg("gpt-4")))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert resp.status_code == 200, resp.text

    # Allow the fire-and-forget background task to run.
    import asyncio

    for _ in range(20):
        await asyncio.sleep(0)
        if log_store.log_request.await_count >= 1:
            break
    log_store.log_request.assert_awaited_once()
    _, kwargs = log_store.log_request.call_args
    actual_keys = set(kwargs.keys())
    missing = EXPECTED_SUCCESS_LOG_KEYS - actual_keys
    extra = actual_keys - EXPECTED_SUCCESS_LOG_KEYS
    assert not missing, (
        f"Missing log_request kwargs: {missing}. PRs B and C must preserve this keyset."
    )
    assert not extra, (
        f"Unexpected new log_request kwargs: {extra}. "
        f"Update EXPECTED_SUCCESS_LOG_KEYS *and* the downstream consumers "
        f"(admin/recent-requests UI, billing reports, alert rules) before "
        f"adding columns."
    )


@pytest.mark.asyncio
async def test_log_payload_error_keyset(monkeypatch):
    """Error path: kwargs to log_request must equal EXPECTED_ERROR_LOG_KEYS."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")

    app, log_store = _build_app(_ErrorAdapter(_mk_cfg("gpt-4")))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert resp.status_code >= 500

    import asyncio

    for _ in range(20):
        await asyncio.sleep(0)
        if log_store.log_request.await_count >= 1:
            break
    log_store.log_request.assert_awaited_once()
    _, kwargs = log_store.log_request.call_args
    actual_keys = set(kwargs.keys())
    missing = EXPECTED_ERROR_LOG_KEYS - actual_keys
    extra = actual_keys - EXPECTED_ERROR_LOG_KEYS
    assert not missing, f"Missing error-path log_request kwargs: {missing}."
    assert not extra, f"Unexpected new error-path log_request kwargs: {extra}."
