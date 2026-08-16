"""api_logs endpoint attribution for the surfaces that bypass FixedRouter.

``LogStore.log_request`` recovers ``api_logs.served_endpoint_id`` from
``metadata["endpoint_id"]``. The chat/completions surface gets that key for
free: ``FixedRouter`` injects ``_routing.endpoint_id`` into the response (and
into the synthetic routing SSE chunk) and the handler merges the blob into
metadata.

``/v1/messages`` picks its adapter itself via ``eligible_adapters``, and
``/v1/embeddings`` looks one up by model id -- neither ever sees a ``_routing``
blob. With no explicit key the column fell through the store's fallback chain
(endpoint_id -> routewise primary -> base_url -> provider) to the bare provider
label, so rows for a local sglang model, its Anthropic-facing alias, and bge-m3
embeddings were all filed under ``"sglang"`` -- a provider, not an endpoint,
and therefore useless for telling two GPU boxes apart.

This is attribution only. Routing and circuit breaking always read
``endpoint_id_for_adapter`` directly and were never affected; the health
recording contracts live in test_anthropic_messages_health_recording.py.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.endpoints import endpoint_id_for_adapter, endpoint_id_for_config
from serving.config.runtime_settings import get_runtime_settings
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import (
    get_completions_logger,
    get_embedding_adapters,
    get_log_store,
    get_operational_store,
)
from serving.servers.embedding_fallback import FallbackEmbeddingAdapter
from serving.servers.routers import embeddings

NATIVE_MODEL = "claude-opus-4.7"
# Shaped like the ids registry._make_provider_id mints ("{model}:{location}"),
# so a passing assertion means the column can distinguish two endpoints of the
# same provider -- which asserting a bare provider label never would.
MESSAGES_ENDPOINT_ID = "claude-opus-4.7:anthropic-api"


# --- /v1/messages ----------------------------------------------------------


def _auth():
    from tests.servers.conftest import ANTHROPIC_TEST_API_KEY

    return {"x-api-key": ANTHROPIC_TEST_API_KEY}


def _body(**overrides):
    body = {
        "model": NATIVE_MODEL,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    }
    body.update(overrides)
    return body


@pytest.fixture
def endpointed_router(anthropic_compat_router):
    """Give the dispatch adapter a real endpoint id, as the registry would.

    The shared fixture builds its ``ModelConfig`` without one, which is exactly
    the state in which ``endpoint_id_for_adapter`` falls back to the provider --
    so the bug would go unnoticed here.
    """
    adapter, _weight = anthropic_compat_router.routes[NATIVE_MODEL].adapters[0]
    adapter.config.endpoint_id = MESSAGES_ENDPOINT_ID
    assert endpoint_id_for_adapter(adapter) == MESSAGES_ENDPOINT_ID
    return anthropic_compat_router


@pytest.fixture
def quiet_alerts(monkeypatch):
    """Keep a tripped breaker from trying to page Slack during a test."""
    from unittest.mock import AsyncMock

    from serving.observability import alerts

    monkeypatch.setattr(alerts, "alert_slack", AsyncMock())
    yield


@pytest.fixture
def captured_log(monkeypatch):
    """Capture the kwargs handed to the surface's log-store scheduler."""
    captured: dict = {}
    from serving.servers.routers import anthropic_messages as amod

    monkeypatch.setattr(
        amod, "_schedule_log_store_task", lambda log_store, **kwargs: captured.update(kwargs)
    )
    return captured


_MESSAGE_START = (
    b"event: message_start\n"
    b'data: {"type":"message_start","message":{"id":"msg_a","model":"claude-opus-4-7",'
    b'"role":"assistant","content":[],"usage":{"input_tokens":4,"output_tokens":0}}}\n\n'
    b"event: content_block_start\n"
    b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
)
_TEXT_DELTA = (
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
)
_MESSAGE_STOP = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


def _fake_session_from_iter(iter_factory):
    """Monkeypatch target for AsyncHTTPClient._ensure_session driving an SSE body."""

    class _FakeContent:
        def iter_any(self):
            return iter_factory()

    class _FakeResp:
        status = 200
        content = _FakeContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class _FakeSession:
        def post(self, url, json=None, headers=None, timeout=None):
            return _FakeResp()

    async def fake_ensure_session(self):
        return _FakeSession()

    return fake_ensure_session


@pytest.mark.asyncio
async def test_messages_non_streaming_logs_the_real_endpoint_id(
    anthropic_test_client, endpointed_router, monkeypatch, captured_log
):
    upstream_resp = {
        "id": "msg_ok",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "Hi"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 1},
    }

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        return upstream_resp

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    r = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())
    assert r.status_code == 200

    assert captured_log["metadata"]["endpoint_id"] == MESSAGES_ENDPOINT_ID
    # The provider label is still logged in its own column; the point is that
    # the endpoint column no longer degrades into a copy of it.
    assert captured_log["provider"] == "anthropic"


@pytest.mark.asyncio
async def test_messages_streaming_logs_the_real_endpoint_id(
    anthropic_test_client, endpointed_router, monkeypatch, captured_log
):
    """Streaming is the bulk of this surface's traffic, and it logs separately.

    The row is written from the generator's ``finally`` block, not the
    non-streaming tail, so it needs its own coverage.
    """

    async def _iter():
        yield _MESSAGE_START + _TEXT_DELTA + _MESSAGE_STOP

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _fake_session_from_iter(_iter))

    async with anthropic_test_client.stream(
        "POST", "/v1/messages", json=_body(stream=True), headers=_auth()
    ) as r:
        assert r.status_code == 200
        async for _ in r.aiter_bytes():
            pass

    assert captured_log["metadata"]["endpoint_id"] == MESSAGES_ENDPOINT_ID


@pytest.mark.asyncio
async def test_messages_failure_row_logs_the_real_endpoint_id(
    anthropic_test_client, endpointed_router, monkeypatch, captured_log, quiet_alerts
):
    """Error rows carry the endpoint too -- that is what names a bad box."""

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        raise aiohttp.ClientResponseError(
            request_info=None, history=None, status=503, message="upstream down"
        )

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    r = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())
    assert r.status_code == 503

    assert captured_log["status_code"] == 503
    assert captured_log["metadata"]["endpoint_id"] == MESSAGES_ENDPOINT_ID


@pytest.mark.asyncio
async def test_messages_endpoint_id_matches_the_health_registry_key(
    anthropic_test_client, endpointed_router, monkeypatch, captured_log
):
    """The logged id and the breaker key must be the same string.

    Cross-referencing a circuit-breaker event against the api_logs rows that
    tripped it is only possible while both sides name the endpoint identically.
    """
    registry = endpointed_router.endpoint_health_registry
    recorded: list[str] = []
    monkeypatch.setattr(registry, "record_success", lambda eid: recorded.append(eid))

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        return {
            "id": "msg_ok",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-4-7",
            "content": [{"type": "text", "text": "Hi"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 1},
        }

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    r = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())
    assert r.status_code == 200

    assert recorded == [captured_log["metadata"]["endpoint_id"]]


# --- /v1/embeddings --------------------------------------------------------


_EMB_PRICING = {"prompt": "0", "completion": "0"}
_EMB_RESPONSE = {
    "object": "list",
    "model": "emb-model",
    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
    "usage": {"prompt_tokens": 5, "total_tokens": 5},
}


class _FakeEmbeddingAdapter:
    def __init__(
        self,
        *,
        provider: str = "sglang",
        endpoint_id: str | None = "bge-m3:local-8005",
        raises: Exception | None = None,
    ) -> None:
        self.config = SimpleNamespace(
            provider=provider,
            endpoint_id=endpoint_id,
            pricing=_EMB_PRICING,
        )
        self._raises = raises

    async def embeddings(self, input_data, **params):
        if self._raises is not None:
            raise self._raises
        return _EMB_RESPONSE


class _CapturingLogger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def schedule_log(self, request_id: str, log_data: dict[str, Any]) -> None:
        self.calls.append((request_id, log_data))


class _FakeRuntimeSettings:
    async def get_bool(self, key: str) -> bool:
        return False


def _build_embeddings_app(adapter, logger: _CapturingLogger) -> FastAPI:
    app = FastAPI()
    app.include_router(embeddings.router)
    app.dependency_overrides[verify_api_key] = lambda: {
        "user_id": "user-emb",
        "authenticated": True,
    }
    app.dependency_overrides[enforce_user_concurrency] = lambda: None
    app.dependency_overrides[get_embedding_adapters] = lambda: {"emb-model": adapter}
    app.dependency_overrides[get_log_store] = lambda: object()  # truthy → logging enabled
    app.dependency_overrides[get_completions_logger] = lambda: logger
    app.dependency_overrides[get_operational_store] = lambda: None
    app.dependency_overrides[get_runtime_settings] = lambda: _FakeRuntimeSettings()
    return app


async def _post_embeddings(app: FastAPI, model: str = "emb-model"):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/embeddings", json={"model": model, "input": "hello"})
    await asyncio.sleep(0.05)
    return resp


@pytest.mark.asyncio
async def test_embeddings_success_logs_the_real_endpoint_id():
    logger = _CapturingLogger()
    app = _build_embeddings_app(_FakeEmbeddingAdapter(), logger)

    resp = await _post_embeddings(app)
    assert resp.status_code == 200

    _, log_data = logger.calls[0]
    assert log_data["metadata"]["endpoint_id"] == "bge-m3:local-8005"
    assert log_data["provider"] == "sglang"


@pytest.mark.asyncio
async def test_embeddings_error_row_logs_the_real_endpoint_id():
    logger = _CapturingLogger()
    app = _build_embeddings_app(_FakeEmbeddingAdapter(raises=RuntimeError("boom")), logger)

    resp = await _post_embeddings(app)
    assert resp.status_code == 500

    _, log_data = logger.calls[0]
    assert log_data["metadata"]["endpoint_id"] == "bge-m3:local-8005"


@pytest.mark.asyncio
async def test_embeddings_fallback_logs_the_endpoint_that_served():
    """When the primary is down the canary served, so the canary is logged.

    ``provider`` already followed ``serving_config`` here; the endpoint id has
    to follow it for the same reason, or a fallback request is attributed to a
    box that never ran it.
    """
    primary = _FakeEmbeddingAdapter(
        endpoint_id="bge-m3:local-8005", raises=RuntimeError("primary down")
    )
    canary = _FakeEmbeddingAdapter(provider="sglang-staging", endpoint_id="bge-m3:staging-8005")
    logger = _CapturingLogger()
    app = _build_embeddings_app(FallbackEmbeddingAdapter([primary, canary]), logger)

    resp = await _post_embeddings(app)
    assert resp.status_code == 200

    _, log_data = logger.calls[0]
    assert log_data["metadata"]["endpoint_id"] == "bge-m3:staging-8005"
    assert log_data["provider"] == "sglang-staging"


@pytest.mark.asyncio
async def test_embeddings_unknown_model_records_no_endpoint_id():
    """No adapter was ever selected, so there is no endpoint to name.

    Leaving the key out lets the store's own fallback chain record the
    ``"router"`` sentinel, which is what a pre-dispatch rejection is.
    """
    logger = _CapturingLogger()
    app = _build_embeddings_app(_FakeEmbeddingAdapter(), logger)

    resp = await _post_embeddings(app, model="does-not-exist")
    assert resp.status_code == 404

    _, log_data = logger.calls[0]
    assert "endpoint_id" not in log_data["metadata"]
    assert log_data["provider"] == "router"


@pytest.mark.asyncio
async def test_embeddings_without_a_configured_endpoint_id_omits_the_key():
    """A config with no endpoint id must not fall back to the provider label.

    Writing ``"sglang"`` into ``metadata["endpoint_id"]`` would short-circuit
    the store's fallback chain and re-create the very row this change fixes.
    """
    logger = _CapturingLogger()
    app = _build_embeddings_app(_FakeEmbeddingAdapter(endpoint_id=None), logger)

    resp = await _post_embeddings(app)
    assert resp.status_code == 200

    _, log_data = logger.calls[0]
    assert "endpoint_id" not in log_data["metadata"]


# --- helper contract -------------------------------------------------------


@pytest.mark.parametrize(
    "config, expected",
    [
        (SimpleNamespace(endpoint_id="bge-m3:local-8005"), "bge-m3:local-8005"),
        (SimpleNamespace(endpoint_id="  bge-m3:local-8005  "), "bge-m3:local-8005"),
        (SimpleNamespace(endpoint_id=None, provider="sglang"), None),
        (SimpleNamespace(endpoint_id="", provider="sglang"), None),
        (SimpleNamespace(endpoint_id="   ", provider="sglang"), None),
        (SimpleNamespace(provider="sglang"), None),
        (None, None),
    ],
)
def test_endpoint_id_for_config_never_substitutes_the_provider(config, expected):
    assert endpoint_id_for_config(config) == expected
