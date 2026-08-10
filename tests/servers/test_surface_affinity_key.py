"""Every dispatching surface publishes a per-caller affinity key.

``KeyPool`` binds a caller to one upstream API key so provider-side prompt
caches stay warm, and ``FixedRouter`` pins the same caller to one provider. Both
read ``req_ctx["affinity_key"]``. ``/v1/messages`` and ``/v1/embeddings`` used to
publish nothing, so every caller on them collapsed onto the process-wide
``_anon`` binding — no stickiness at all, worst on ``/v1/messages``, which
carries prefill-dominated Claude Code traffic.

These tests drive the handlers and read the context the adapter actually sees.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.config.runtime_settings import get_runtime_settings
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import (
    get_completions_logger,
    get_embedding_adapters,
    get_log_store,
    get_operational_store,
)
from serving.servers.routers import embeddings
from serving.utils import context as req_ctx

NATIVE_MODEL = "claude-opus-4.7"

_ANTHROPIC_RESPONSE = {
    "id": "msg_affinity",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-4-7",
    "content": [{"type": "text", "text": "hi"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


# --- /v1/embeddings --------------------------------------------------------


class _CapturingEmbeddingAdapter:
    """Records the request context each dispatch runs under."""

    def __init__(self) -> None:
        self.config = SimpleNamespace(provider="fake-provider", pricing=None)
        self.contexts: list[dict[str, Any]] = []

    async def embeddings(self, input_data, **params):
        self.contexts.append(dict(req_ctx.get()))
        return {
            "object": "list",
            "model": "emb-model",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }


def _embeddings_app(adapter: _CapturingEmbeddingAdapter, user_ctx: dict[str, Any]) -> FastAPI:
    app = FastAPI()
    app.include_router(embeddings.router)
    app.dependency_overrides[verify_api_key] = lambda: user_ctx
    app.dependency_overrides[enforce_user_concurrency] = lambda: None
    app.dependency_overrides[get_embedding_adapters] = lambda: {"emb-model": adapter}
    app.dependency_overrides[get_log_store] = lambda: None
    app.dependency_overrides[get_completions_logger] = lambda: None
    app.dependency_overrides[get_operational_store] = lambda: None
    app.dependency_overrides[get_runtime_settings] = lambda: None
    return app


async def _embed_as(adapter: _CapturingEmbeddingAdapter, user_ctx: dict[str, Any]) -> None:
    app = _embeddings_app(adapter, user_ctx)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/embeddings", json={"model": "emb-model", "input": "hi"})
    assert resp.status_code == 200


async def test_embeddings_publishes_the_callers_key_hash():
    adapter = _CapturingEmbeddingAdapter()

    await _embed_as(adapter, {"user_id": "u-1", "authenticated": True, "auth_key_hash": "hash-a"})

    ctx = adapter.contexts[0]
    assert ctx["affinity_key"] == "hash-a"
    assert ctx["auth_key_hash"] == "hash-a"


async def test_embeddings_falls_back_to_an_ip_key_when_unauthenticated():
    """Auth-disabled deployments still get a per-caller key rather than ``_anon``."""
    adapter = _CapturingEmbeddingAdapter()

    await _embed_as(adapter, {"user_id": "anonymous", "authenticated": False})

    ctx = adapter.contexts[0]
    assert ctx["affinity_key"].startswith("ip:")
    assert ctx["auth_key_hash"] == "_anon"


async def test_embeddings_callers_do_not_share_an_affinity_key():
    adapter = _CapturingEmbeddingAdapter()

    await _embed_as(adapter, {"user_id": "u-1", "authenticated": True, "auth_key_hash": "hash-a"})
    await _embed_as(adapter, {"user_id": "u-2", "authenticated": True, "auth_key_hash": "hash-b"})

    assert [c["affinity_key"] for c in adapter.contexts] == ["hash-a", "hash-b"]


# --- /v1/messages ----------------------------------------------------------


@pytest.fixture
def anthropic_captures(anthropic_test_app, monkeypatch):
    """Capture the request context every ``/v1/messages`` dispatch runs under."""
    from serving.http import AsyncHTTPClient

    captured: list[dict[str, Any]] = []

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        captured.append(dict(req_ctx.get()))
        return _ANTHROPIC_RESPONSE

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)
    return captured


def _authenticate_as(app: FastAPI, user_ctx: dict[str, Any]) -> None:
    app.dependency_overrides[verify_api_key] = lambda: user_ctx


async def _message_as(
    app: FastAPI,
    client: AsyncClient,
    user_ctx: dict[str, Any],
) -> None:
    _authenticate_as(app, user_ctx)
    resp = await client.post(
        "/v1/messages",
        json={
            "model": NATIVE_MODEL,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200


async def test_v1_messages_publishes_the_callers_key_hash(
    anthropic_test_app, anthropic_test_client, anthropic_captures
):
    await _message_as(
        anthropic_test_app,
        anthropic_test_client,
        {"authenticated": True, "user_id": "u-1", "role": "internal", "auth_key_hash": "hash-a"},
    )

    ctx = anthropic_captures[0]
    assert ctx["affinity_key"] == "hash-a"
    assert ctx["auth_key_hash"] == "hash-a"


async def test_v1_messages_falls_back_to_an_ip_key_when_unauthenticated(
    anthropic_test_app, anthropic_test_client, anthropic_captures
):
    await _message_as(
        anthropic_test_app,
        anthropic_test_client,
        {"authenticated": False, "user_id": "anonymous", "role": "internal"},
    )

    ctx = anthropic_captures[0]
    assert ctx["affinity_key"].startswith("ip:")
    assert ctx["auth_key_hash"] == "_anon"


async def test_v1_messages_callers_do_not_share_an_affinity_key(
    anthropic_test_app, anthropic_test_client, anthropic_captures
):
    for key_hash in ("hash-a", "hash-b"):
        await _message_as(
            anthropic_test_app,
            anthropic_test_client,
            {
                "authenticated": True,
                "user_id": f"u-{key_hash}",
                "role": "internal",
                "auth_key_hash": key_hash,
            },
        )

    assert [c["affinity_key"] for c in anthropic_captures] == ["hash-a", "hash-b"]
