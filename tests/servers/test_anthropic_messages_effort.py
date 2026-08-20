"""Tests for reasoning-effort translation in the Anthropic Messages router.

Claude Code's ``--effort`` arrives as ``output_config.effort``. It reaches an
OpenAI-compatible upstream as ``reasoning_effort`` when the resolved model both
supports the parameter and declares the value; every other case drops it
*visibly* rather than forwarding a value the upstream would 400 on.

The regression these guard: before this translation existed the field was not
known by name here at all, so the effort went out with the sanitizer's bulk
drop and was not even named in its warning.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from tests.servers.conftest import ANTHROPIC_TEST_API_KEY

REASONING_MODEL = "test-reasoner"
PLAIN_MODEL = "test-plain"
#: Deliberately not low/medium/high: glm-5.3's real domain has no ``medium``,
#: and a test that used the intuitive three would pass against an
#: implementation that assumed them.
DECLARED_EFFORTS = ["low", "high", "max"]


def _auth():
    return {"x-api-key": ANTHROPIC_TEST_API_KEY}


@pytest_asyncio.fixture
async def effort_client(mock_db_logger, mock_operational_store, mock_log_store):
    """Anthropic test client with one effort-capable model and one without."""
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
        supported_params=["temperature", "max_tokens", "stream", "reasoning_effort"],
        reasoning_efforts=DECLARED_EFFORTS,
    )
    plain_cfg = ModelConfig(
        id=PLAIN_MODEL,
        name="Test Plain",
        provider="sglang",
        base_url="https://example-plain.test",
        api_key="p-test",
        chat_path="/chat/completions",
        max_output_length=8192,
        supported_params=["temperature", "max_tokens", "stream"],
    )

    re = RouteExecutor()
    re.register_route(REASONING_MODEL, [(OpenAICompatAdapter(reasoner_cfg), 1.0)])
    re.register_route(PLAIN_MODEL, [(OpenAICompatAdapter(plain_cfg), 1.0)])

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

    app = FastAPI(title="Effort Test App", lifespan=lifespan)
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


def _body(model: str, **extra):
    return {
        "model": model,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hi"}],
        **extra,
    }


@pytest.mark.asyncio
async def test_declared_effort_reaches_upstream(effort_client, monkeypatch):
    """The whole point: --effort low arrives as reasoning_effort=low."""
    captured = _patch_upstream(monkeypatch)
    body = _body(REASONING_MODEL, output_config={"effort": "low"})

    r = await effort_client.post("/v1/messages", json=body, headers=_auth())

    assert r.status_code == 200
    assert captured["json"]["reasoning_effort"] == "low"
    # The Anthropic spelling must not also ride along to an OpenAI upstream.
    assert "output_config" not in captured["json"]


@pytest.mark.asyncio
async def test_undeclared_effort_is_dropped_not_forwarded(effort_client, monkeypatch):
    """``medium`` is a real Anthropic level and not a glm-5.3 one.

    Forwarding it would make the upstream 400 a request whose only fault is a
    CLI flag nobody can trace; clamping it to a neighbour would silently change
    what the user asked for. It is dropped, and the turn still runs.
    """
    captured = _patch_upstream(monkeypatch)
    body = _body(REASONING_MODEL, output_config={"effort": "medium"})

    r = await effort_client.post("/v1/messages", json=body, headers=_auth())

    assert r.status_code == 200
    assert "reasoning_effort" not in captured["json"]


@pytest.mark.asyncio
async def test_effort_dropped_for_model_without_the_parameter(effort_client, monkeypatch):
    captured = _patch_upstream(monkeypatch)
    body = _body(PLAIN_MODEL, output_config={"effort": "low"})

    r = await effort_client.post("/v1/messages", json=body, headers=_auth())

    assert r.status_code == 200
    assert "reasoning_effort" not in captured["json"]


@pytest.mark.asyncio
async def test_no_effort_sends_no_parameter(effort_client, monkeypatch):
    """Auto stays auto: an unset effort adds nothing to the upstream body."""
    captured = _patch_upstream(monkeypatch)

    r = await effort_client.post("/v1/messages", json=_body(REASONING_MODEL), headers=_auth())

    assert r.status_code == 200
    assert "reasoning_effort" not in captured["json"]


@pytest.mark.asyncio
async def test_client_supplied_top_level_effort_meets_the_same_check(effort_client, monkeypatch):
    """A caller spelling it the OpenAI way does not get to skip the domain check."""
    captured = _patch_upstream(monkeypatch)

    r = await effort_client.post(
        "/v1/messages", json=_body(REASONING_MODEL, reasoning_effort="medium"), headers=_auth()
    )
    assert r.status_code == 200
    assert "reasoning_effort" not in captured["json"]

    r = await effort_client.post(
        "/v1/messages", json=_body(REASONING_MODEL, reasoning_effort="max"), headers=_auth()
    )
    assert r.status_code == 200
    assert captured["json"]["reasoning_effort"] == "max"


@pytest.mark.asyncio
async def test_output_config_is_named_in_the_dropped_warning(effort_client, monkeypatch, caplog):
    """What is left of output_config after the effort is read must not vanish silently."""
    _patch_upstream(monkeypatch)
    body = _body(REASONING_MODEL, output_config={"format": {"type": "json_schema"}})

    with caplog.at_level("WARNING"):
        r = await effort_client.post("/v1/messages", json=body, headers=_auth())

    assert r.status_code == 200
    assert any(
        "output_config" in record.message and "Dropped Anthropic-only fields" in record.message
        for record in caplog.records
    )
