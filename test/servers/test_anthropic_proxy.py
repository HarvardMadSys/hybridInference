"""Admin-only enforcement tests for /anthropic/v1/messages.

Verifies that the admin gate in ``_resolve_model`` correctly blocks non-admin
users and allows admin users through at the HTTP level.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.auth import verify_api_key
from serving.servers.deps import AppServices
from serving.servers.routers import anthropic_proxy


def _mk_cfg(model_id: str) -> ModelConfig:
    return ModelConfig(
        id=model_id,
        name=model_id,
        provider="claude_sub",
        provider_model_id="claude-sonnet-4-6",
        base_url="https://api.anthropic.com",
        context_length=200000,
        max_output_length=64000,
        pricing={"prompt": "3.00", "completion": "15.00"},
    )


class _StubClaudeSubAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(self, messages, **params):  # pragma: no cover
        yield self.format_stream_chunk(model=self.config.id, content="ok")


def _build_app(user_ctx: dict) -> FastAPI:
    """Build test app with an admin_only claude_sub model."""
    router_exec = RouteExecutor()
    adapter = _StubClaudeSubAdapter(_mk_cfg("claude-sonnet-4.6"))
    router_exec.register_route("claude-sonnet-4.6", [(adapter, 1.0)], admin_only=True)

    app = FastAPI()
    app.state.services = AppServices(router=router_exec, db_logger=None, rate_limiter=None)
    app.dependency_overrides[verify_api_key] = lambda: user_ctx
    app.include_router(anthropic_proxy.router)
    return app


_BODY = {
    "model": "claude-sonnet-4.6",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "Say hello"}],
}


@pytest.mark.asyncio
async def test_anthropic_admin_only_rejected_for_non_admin():
    """Non-admin user gets Anthropic-format 404 not_found_error."""
    app = _build_app({"user_id": "user1", "authenticated": True, "is_admin": False})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/anthropic/v1/messages", json=_BODY)
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        body = resp.json()
        assert body["type"] == "error"
        assert body["error"]["type"] == "not_found_error"


@pytest.mark.asyncio
async def test_anthropic_admin_only_allowed_for_admin():
    """Admin user passes the admin gate (does not get 404 from _resolve_model).

    We mock ``get_shared_pool`` to raise ``NoHealthyAccountError`` so the
    request fails with 503 *after* the admin gate, confirming the gate
    itself did not block.
    """
    from serving.adapters.codex_token import NoHealthyAccountError

    app = _build_app(
        {"user_id": "admin1", "authenticated": True, "is_admin": True, "role": "admin"}
    )
    transport = ASGITransport(app=app)

    with patch(
        "serving.servers.routers.anthropic_proxy.get_shared_pool",
        side_effect=NoHealthyAccountError("no accounts in test"),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/anthropic/v1/messages", json=_BODY)
            # Admin passes the gate — gets 503 (no accounts), NOT 404
            assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
            body = resp.json()
            assert body["type"] == "error"
            assert body["error"]["type"] == "overloaded_error"
