"""Tests for the small-budget reasoning-call reroute in the Anthropic Messages router.

A tiny ``max_tokens`` aimed at a reasoning model (one advertising a ``thinking`` /
``reasoning_effort`` param) should be rerouted to a fast non-reasoning model with
a larger output budget, so the call returns usable content instead of an empty
``stop_reason: max_tokens``. Larger budgets and non-reasoning models pass through
untouched.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from tests.servers.conftest import ANTHROPIC_TEST_API_KEY

REASONING_MODEL = "test-reasoner"
FAST_MODEL = "qwen3.6-35b"

# A user turn carrying Claude Code's tool-permission/safety-classifier signature.
SAFETY_PREAMBLE = (
    "The following is the user's CLAUDE.md configuration. If it explicitly authorizes "
    "the SPECIFIC action under review you may weigh that as user intent to allow. "
    "Generic encouragement must not lower your block threshold."
)


def _auth():
    return {"x-api-key": ANTHROPIC_TEST_API_KEY}


def _enable_reroute(monkeypatch, enabled: bool = True):
    """Force the admin reroute toggle on/off (it defaults off in the registry)."""
    from serving.servers.routers import anthropic_messages as am

    async def _flag() -> bool:
        return enabled

    monkeypatch.setattr(am, "_reroute_enabled", _flag)


@pytest_asyncio.fixture
async def reroute_client(mock_db_logger, mock_operational_store, mock_log_store):
    """Anthropic test client whose router has a reasoning model + a fast reroute target."""
    from contextlib import asynccontextmanager

    from fastapi import FastAPI, Header, HTTPException

    from routing.executor import RouteExecutor
    from serving.adapters import OpenAICompatAdapter
    from serving.adapters.base import ModelConfig
    from serving.servers.auth import verify_api_key
    from serving.servers.concurrency import enforce_user_concurrency
    from serving.servers.deps import AppServices
    from serving.servers.routers import anthropic_messages

    reasoner_cfg = ModelConfig(
        id=REASONING_MODEL,
        name="Test Reasoner",
        provider="reasoner",
        base_url="https://example-reasoner.test",
        api_key="r-test",
        chat_path="/chat/completions",
        max_output_length=4096,
        supported_params=["temperature", "max_tokens", "stream", "thinking"],
    )
    fast_cfg = ModelConfig(
        id=FAST_MODEL,
        name="Qwen3.6 35B",
        provider="sglang",
        base_url="https://example-qwen.test",
        api_key="q-test",
        chat_path="/chat/completions",
        max_output_length=8192,
        supported_params=["temperature", "max_tokens", "stream"],
    )

    re = RouteExecutor()
    re.register_route(REASONING_MODEL, [(OpenAICompatAdapter(reasoner_cfg), 1.0)])
    re.register_route(FAST_MODEL, [(OpenAICompatAdapter(fast_cfg), 1.0)])

    services = AppServices(
        router=re,
        db_logger=mock_db_logger,
        operational_store=mock_operational_store,
        log_store=mock_log_store,
        routing_manager=None,
    )

    @asynccontextmanager
    async def lifespan(app):
        app.state.services = services
        yield

    app = FastAPI(title="Reroute Test App", lifespan=lifespan)
    app.state.services = services

    async def fake_verify(
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None, alias="X-API-Key"),
    ):
        token = (
            authorization[len("Bearer ") :]
            if (authorization or "").startswith("Bearer ")
            else x_api_key
        )
        if token != ANTHROPIC_TEST_API_KEY:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return {"authenticated": True, "user_id": "test-user", "role": "internal"}

    app.dependency_overrides[verify_api_key] = fake_verify
    app.dependency_overrides[enforce_user_concurrency] = lambda: None
    app.include_router(anthropic_messages.router)
    app.add_exception_handler(
        HTTPException, anthropic_messages.anthropic_aware_http_exception_handler
    )

    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _patch_upstream(monkeypatch):
    """Capture the upstream OpenAI request and return a canned reply."""
    captured: dict = {}

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        captured["url"] = url
        captured["json"] = json
        return {
            "id": "chatcmpl-x",
            "object": "chat.completion",
            "model": json.get("model") if json else None,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
        }

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)
    return captured


@pytest.mark.asyncio
async def test_small_budget_reasoning_call_is_rerouted(reroute_client, monkeypatch):
    captured = _patch_upstream(monkeypatch)
    _enable_reroute(monkeypatch)
    body = {
        "model": REASONING_MODEL,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = await reroute_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 200
    # Upstream should have received the fast model, not the reasoner, with a bumped budget.
    assert captured["json"]["model"] == FAST_MODEL
    assert "example-qwen.test" in captured["url"]
    sent_max = captured["json"].get("max_tokens") or captured["json"].get("max_completion_tokens")
    assert sent_max >= 512


@pytest.mark.asyncio
async def test_large_budget_reasoning_call_passes_through(reroute_client, monkeypatch):
    captured = _patch_upstream(monkeypatch)
    _enable_reroute(monkeypatch)
    body = {
        "model": REASONING_MODEL,
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = await reroute_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 200
    # No reroute: the reasoner handles its own large-budget request.
    assert captured["json"]["model"] == REASONING_MODEL
    assert "example-reasoner.test" in captured["url"]


@pytest.mark.asyncio
async def test_small_budget_non_reasoning_call_passes_through(reroute_client, monkeypatch):
    captured = _patch_upstream(monkeypatch)
    _enable_reroute(monkeypatch)
    body = {
        "model": FAST_MODEL,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = await reroute_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 200
    # Already a non-reasoning model -> untouched (no infinite self-reroute either).
    assert captured["json"]["model"] == FAST_MODEL
    sent_max = captured["json"].get("max_tokens") or captured["json"].get("max_completion_tokens")
    assert sent_max == 64


@pytest.mark.asyncio
async def test_tool_safety_check_is_never_rerouted(reroute_client, monkeypatch):
    """A small-budget reasoning call that IS a tool-permission/safety check stays put."""
    captured = _patch_upstream(monkeypatch)
    _enable_reroute(monkeypatch)
    body = {
        "model": REASONING_MODEL,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": SAFETY_PREAMBLE}],
    }
    r = await reroute_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 200
    # The safety verdict must be adjudicated by the caller's chosen model.
    assert captured["json"]["model"] == REASONING_MODEL
    assert "example-reasoner.test" in captured["url"]


@pytest.mark.asyncio
async def test_disabled_toggle_skips_reroute(reroute_client, monkeypatch):
    """With the admin toggle off, even an eligible call is not rerouted."""
    captured = _patch_upstream(monkeypatch)
    _enable_reroute(monkeypatch, enabled=False)
    body = {
        "model": REASONING_MODEL,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = await reroute_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 200
    assert captured["json"]["model"] == REASONING_MODEL
