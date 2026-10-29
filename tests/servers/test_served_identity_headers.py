"""X-FI-* served-identity response headers (issue #1504).

The gateway resolves the served adapter, endpoint and provider before it
dispatches, but that identity never reached callers: ``owned_by`` is only a
provider label and the small-budget Anthropic reroute can serve a different
model than the one the client named. These tests pin the sanitized header set
on both inference surfaces, streaming and non-streaming, and the rule that
auto-selection's deliberate concealment of the served model extends to the
headers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter as RouteExecutor
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import completions
from serving.stream import done_sentinel, make_final_usage_chunk

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

NATIVE_MODEL = "claude-opus-4.7"
CHAT_MODEL = "glm-4.7"
_PRIVATE_ROUTE_LABEL = "gpu" + "7" + "-api"
_PRIVATE_MODEL_LABEL = "private" + ".internal"


def _anthropic_auth():
    from tests.servers.conftest import ANTHROPIC_TEST_API_KEY

    return {"x-api-key": ANTHROPIC_TEST_API_KEY}


def _enable_trusted_probe(app):
    """Give this test app the same authenticated diagnostic identity as a probe."""
    from serving.servers.auth import verify_api_key

    app.dependency_overrides[verify_api_key] = lambda: {
        "authenticated": True,
        "is_admin": True,
        "role": "internal",
        "user_id": "trusted-probe",
        "user_name": "trusted-probe",
    }


def _enable_served_identity_diagnostic(app):
    """Model an auth dependency that attached an explicit server entitlement."""
    from serving.servers.auth import verify_api_key

    app.dependency_overrides[verify_api_key] = lambda: {
        "authenticated": True,
        "is_admin": False,
        "role": "free",
        "user_id": "diagnostic-caller",
        "diagnostic_entitlements": frozenset({"served_identity"}),
    }


def _anthropic_body(**overrides):
    body = {
        "model": NATIVE_MODEL,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    }
    body.update(overrides)
    return body


class DummyAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        resp = self.format_response(content="Test response", model=self.config.id)
        resp["_routing"] = {
            "provider": self.config.provider,
            "endpoint_id": self.config.endpoint_id,
            "base_url": self.config.base_url,
        }
        return resp

    async def stream_chat_completion(self, messages, **params):
        yield self.format_stream_chunk(model=self.config.id, content="Test ")
        yield make_final_usage_chunk(
            model=self.config.id, messages=messages, total_content="Test response"
        )
        yield done_sentinel()


def _chat_cfg(model_id: str) -> ModelConfig:
    return ModelConfig(
        id=model_id,
        name=model_id,
        provider="zai",
        base_url="https://example-zai.test",
        chat_path="/chat/completions",
        endpoint_id=f"{model_id}:zai-api",
        endpoint_identity_publishable=True,
        context_length=8192,
        max_output_length=4096,
        supported_params=["temperature", "top_p", "max_tokens", "stream", "tools"],
    )


@pytest.fixture
def chat_app(monkeypatch, mock_db_logger, mock_log_store) -> FastAPI:
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    router = RouteExecutor()
    router.register_route(CHAT_MODEL, [(DummyAdapter(_chat_cfg(CHAT_MODEL)), 1.0)])

    app = FastAPI(title="Test Served Identity App")
    app.state.services = AppServices(
        router=router, db_logger=mock_db_logger, log_store=mock_log_store
    )
    install_error_handlers(app)
    app.include_router(completions.router)
    return app


@pytest.fixture
async def chat_client(chat_app) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=chat_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def anthropic_client(anthropic_test_app) -> AsyncClient:
    transport = ASGITransport(app=anthropic_test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _patch_anthropic_upstream(monkeypatch, model: str = "claude-opus-4-7"):
    from serving.http import AsyncHTTPClient

    upstream_resp = {
        "id": "msg_hdr",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "Hi"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 1},
    }

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        return upstream_resp

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)


# ---------------------------------------------------------------------------
# Anthropic /v1/messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity_headers",
    [
        {"x-probe": "synthetic"},
        {"x-fi-served-identity": "include"},
    ],
    ids=["trusted-probe", "authorized-opt-in"],
)
async def test_messages_response_carries_served_identity_headers(
    anthropic_client, anthropic_test_app, monkeypatch, identity_headers
):
    _patch_anthropic_upstream(monkeypatch)
    adapter = anthropic_test_app.state.services.router.routes[NATIVE_MODEL].adapters[0][0]
    # The route's registry-minted identity, as production configs carry it.
    adapter.config.endpoint_id = f"{NATIVE_MODEL}:anthropic-api"
    adapter.config.endpoint_identity_publishable = True
    if "x-fi-served-identity" in identity_headers:
        _enable_served_identity_diagnostic(anthropic_test_app)
    else:
        _enable_trusted_probe(anthropic_test_app)

    r = await anthropic_client.post(
        "/v1/messages",
        json=_anthropic_body(),
        headers={**_anthropic_auth(), **identity_headers},
    )
    assert r.status_code == 200
    assert r.headers["x-fi-requested-model"] == NATIVE_MODEL
    assert r.headers["x-fi-served-model"] == NATIVE_MODEL
    assert r.headers["x-fi-served-endpoint"] == f"{NATIVE_MODEL}:anthropic-api"
    assert r.headers["x-fi-served-provider"] == adapter.config.provider


@pytest.mark.asyncio
async def test_messages_with_non_latin_model_id_does_not_fail_or_leak_headers(
    anthropic_client, anthropic_test_app, monkeypatch
):
    _patch_anthropic_upstream(monkeypatch)
    model = "qwen-通义"
    adapter = anthropic_test_app.state.services.router.routes[NATIVE_MODEL].adapters[0][0]
    adapter.config.endpoint_identity_publishable = True
    anthropic_test_app.state.services.router.register_route(model, [(adapter, 1.0)])
    _enable_trusted_probe(anthropic_test_app)

    response = await anthropic_client.post(
        "/v1/messages",
        json=_anthropic_body(model=model),
        headers={**_anthropic_auth(), "x-probe": "synthetic"},
    )

    assert response.status_code == 200
    assert "x-fi-requested-model" not in response.headers
    assert "x-fi-served-model" not in response.headers
    assert "x-fi-served-endpoint" not in response.headers
    assert response.headers["x-fi-served-provider"] == adapter.config.provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity_headers",
    [
        {"x-probe": "synthetic"},
        {"x-fi-served-identity": "include"},
    ],
    ids=["trusted-probe", "authorized-opt-in"],
)
async def test_messages_stream_carries_the_same_served_identity_headers(
    anthropic_client, anthropic_test_app, monkeypatch, identity_headers
):
    """Streaming and non-streaming expose the same identity (issue #1504)."""

    class _FakeContent:
        def iter_any(self):
            async def _chunks():
                yield (
                    b"event: message_start\n"
                    b'data: {"type":"message_start","message":{"id":"msg_h","model":"claude-opus-4-7",'
                    b'"role":"assistant","content":[],"usage":{"input_tokens":4,"output_tokens":0}}}\n\n'
                    b"event: content_block_delta\n"
                    b'data: {"type":"content_block_delta","index":0,'
                    b'"delta":{"type":"text_delta","text":"hi"}}\n\n'
                    b"event: message_stop\n"
                    b'data: {"type":"message_stop"}\n\n'
                )

            return _chunks()

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

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", fake_ensure_session)

    adapter = anthropic_test_app.state.services.router.routes[NATIVE_MODEL].adapters[0][0]
    adapter.config.endpoint_id = f"{NATIVE_MODEL}:anthropic-api"
    adapter.config.endpoint_identity_publishable = True
    if "x-fi-served-identity" in identity_headers:
        _enable_served_identity_diagnostic(anthropic_test_app)
    else:
        _enable_trusted_probe(anthropic_test_app)
    async with anthropic_client.stream(
        "POST",
        "/v1/messages",
        json=_anthropic_body(stream=True),
        headers={**_anthropic_auth(), **identity_headers},
    ) as r:
        assert r.status_code == 200
        assert r.headers["x-fi-requested-model"] == NATIVE_MODEL
        assert r.headers["x-fi-served-model"] == NATIVE_MODEL
        assert r.headers["x-fi-served-endpoint"] == adapter.config.endpoint_id
        assert r.headers["x-fi-served-provider"] == adapter.config.provider
        body = b"".join([chunk async for chunk in r.aiter_bytes()])
    assert b"message_stop" in body


# ---------------------------------------------------------------------------
# OpenAI /v1/chat/completions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_completions_response_carries_served_identity_headers(chat_client):
    _enable_trusted_probe(chat_client._transport.app)
    r = await chat_client.post(
        "/v1/chat/completions",
        json={"model": CHAT_MODEL, "messages": [{"role": "user", "content": "Hi"}]},
        headers={"x-probe": "synthetic"},
    )
    assert r.status_code == 200
    assert r.headers["x-fi-requested-model"] == CHAT_MODEL
    assert r.headers["x-fi-served-model"] == CHAT_MODEL
    assert r.headers["x-fi-served-endpoint"] == f"{CHAT_MODEL}:zai-api"
    assert r.headers["x-fi-served-provider"] == "zai"
    assert r.headers["x-fi-served-endpoint"].count("http") == 0


@pytest.mark.asyncio
async def test_authorized_opt_in_discloses_chat_identity_without_marking_probe(chat_client):
    app = chat_client._transport.app
    _enable_served_identity_diagnostic(app)
    response = await chat_client.post(
        "/v1/chat/completions",
        json={"model": CHAT_MODEL, "messages": [{"role": "user", "content": "Hi"}]},
        headers={"x-fi-served-identity": "include"},
    )

    assert response.status_code == 200
    assert response.headers["x-fi-served-model"] == CHAT_MODEL
    assert response.headers["x-fi-served-endpoint"] == f"{CHAT_MODEL}:zai-api"
    # Opt-in is disclosure only; it does not turn an ordinary request into a probe.
    assert chat_client._transport.app.state.services.log_store is not None


@pytest.mark.asyncio
async def test_internal_role_alone_does_not_authorize_identity_opt_in(chat_client):
    app = chat_client._transport.app
    _enable_trusted_probe(app)

    response = await chat_client.post(
        "/v1/chat/completions",
        json={"model": CHAT_MODEL, "messages": [{"role": "user", "content": "Hi"}]},
        headers={"x-fi-served-identity": "include"},
    )

    assert response.status_code == 200
    assert not any(name.lower().startswith("x-fi-") for name in response.headers)


@pytest.mark.asyncio
async def test_alias_request_publishes_canonical_model_and_endpoint(chat_client):
    """An alias remains the requested spelling; identity uses the canonical id."""
    chat_client._transport.app.state.services.router.register_route(
        CHAT_MODEL,
        [(chat_client._transport.app.state.services.router.routes[CHAT_MODEL].adapters[0][0], 1.0)],
        aliases=["glm-latest"],
    )
    _enable_served_identity_diagnostic(chat_client._transport.app)

    response = await chat_client.post(
        "/v1/chat/completions",
        json={"model": "glm-latest", "messages": [{"role": "user", "content": "Hi"}]},
        headers={"x-fi-served-identity": "include"},
    )

    assert response.status_code == 200
    assert response.headers["x-fi-requested-model"] == "glm-latest"
    assert response.headers["x-fi-served-model"] == CHAT_MODEL
    assert response.headers["x-fi-served-endpoint"] == f"{CHAT_MODEL}:zai-api"
    assert response.headers["x-fi-served-provider"] == "zai"


@pytest.mark.asyncio
async def test_untrusted_customer_does_not_receive_served_identity_headers(chat_client):
    response = await chat_client.post(
        "/v1/chat/completions",
        json={"model": CHAT_MODEL, "messages": [{"role": "user", "content": "Hi"}]},
    )
    assert response.status_code == 200
    assert not any(name.lower().startswith("x-fi-") for name in response.headers)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"x-fi-served-identity": "include"},
        {"x-probe": "synthetic"},
        {"x-fi-served-identity": "include", "x-probe": "synthetic"},
    ],
    ids=["opt-in-only", "probe-only", "both-spoofed"],
)
async def test_opt_in_header_does_not_grant_identity_disclosure(chat_client, headers):
    response = await chat_client.post(
        "/v1/chat/completions",
        json={"model": CHAT_MODEL, "messages": [{"role": "user", "content": "Hi"}]},
        headers=headers,
    )

    assert response.status_code == 200
    assert not any(name.lower().startswith("x-fi-") for name in response.headers)


@pytest.mark.asyncio
async def test_non_latin_model_id_does_not_fail_chat_or_stream(chat_client):
    model = "qwen-通义"
    private_host = "gpu" + "7" + ".cluster." + "internal"
    config = ModelConfig(
        id=model,
        name=model,
        provider="sglang",
        base_url=f"http://{private_host}:8000/v1",
        endpoint_id=f"{model}:{_PRIVATE_ROUTE_LABEL}",
        endpoint_identity_publishable=False,
    )
    chat_client._transport.app.state.services.router.register_route(
        model, [(DummyAdapter(config), 1.0)]
    )
    _enable_trusted_probe(chat_client._transport.app)
    headers = {"x-probe": "synthetic"}
    for stream in (False, True):
        response = await chat_client.post(
            "/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": stream,
            },
            headers=headers,
        )
        assert response.status_code == 200
        assert "x-fi-requested-model" not in response.headers
        assert "x-fi-served-model" not in response.headers
        assert "x-fi-served-endpoint" not in response.headers
        assert response.headers["x-fi-served-provider"] == "sglang"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity_headers",
    [
        {"x-probe": "synthetic"},
        {"x-fi-served-identity": "include"},
    ],
    ids=["trusted-probe", "authorized-opt-in"],
)
async def test_chat_completions_stream_carries_requested_identity(chat_client, identity_headers):
    """A trusted diagnostic stream carries the selected route identity."""
    if "x-fi-served-identity" in identity_headers:
        _enable_served_identity_diagnostic(chat_client._transport.app)
    else:
        _enable_trusted_probe(chat_client._transport.app)
    r = await chat_client.post(
        "/v1/chat/completions",
        json={
            "model": CHAT_MODEL,
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        },
        headers=identity_headers,
    )
    assert r.status_code == 200
    assert r.headers["x-fi-requested-model"] == CHAT_MODEL
    assert r.headers["x-fi-served-model"] == CHAT_MODEL
    assert r.headers["x-fi-served-endpoint"] == f"{CHAT_MODEL}:zai-api"
    assert r.headers["x-fi-served-provider"] == "zai"


@pytest.mark.asyncio
async def test_forced_streaming_branch_carries_the_headers(chat_app):
    """The keepalive branch returns its StreamingResponse directly, so the
    headers must ride on that object — injected-parameter headers are not
    merged into a returned Response."""

    class _Forced:
        async def get_bool(self, key: str) -> bool:
            return key == "force_chat_completions_streaming"

    from serving.config.runtime_settings import get_runtime_settings

    chat_app.dependency_overrides[get_runtime_settings] = lambda: _Forced()
    _enable_trusted_probe(chat_app)

    transport = ASGITransport(app=chat_app)
    from httpx import AsyncClient as _AC

    async with _AC(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/chat/completions",
            json={"model": CHAT_MODEL, "messages": [{"role": "user", "content": "Hi"}]},
            headers={"x-probe": "synthetic"},
        )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["x-fi-requested-model"] == CHAT_MODEL
    assert r.headers["x-fi-served-model"] == CHAT_MODEL
    assert r.headers["x-fi-served-endpoint"] == f"{CHAT_MODEL}:zai-api"
    assert r.headers["x-fi-served-provider"] == "zai"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity_headers",
    [
        {"x-probe": "synthetic"},
        {"x-fi-served-identity": "include"},
    ],
    ids=["trusted-probe", "authorized-opt-in"],
)
async def test_multi_backend_streaming_uses_observed_route_identity(chat_app, identity_headers):
    """Authorized diagnostic paths receive the selected stream route identity."""

    class _SecondAdapter(DummyAdapter):
        pass

    second_cfg = _chat_cfg(CHAT_MODEL)
    second_cfg.provider = "sglang"
    second_cfg.endpoint_id = f"{CHAT_MODEL}:{_PRIVATE_ROUTE_LABEL}"
    second_cfg.endpoint_identity_publishable = False
    second = _SecondAdapter(second_cfg)
    chat_app.state.services.router.register_route(
        CHAT_MODEL,
        [(chat_app.state.services.router.routes[CHAT_MODEL].adapters[0][0], 0.5), (second, 0.5)],
    )
    transport = ASGITransport(app=chat_app)
    if "x-fi-served-identity" in identity_headers:
        _enable_served_identity_diagnostic(chat_app)
    else:
        _enable_trusted_probe(chat_app)
    from httpx import AsyncClient as _AC

    async with _AC(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/chat/completions",
            json={
                "model": CHAT_MODEL,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
            headers=identity_headers,
        )
    assert r.status_code == 200
    assert r.headers["x-fi-requested-model"] == CHAT_MODEL
    assert r.headers["x-fi-served-model"] == CHAT_MODEL
    assert r.headers["x-fi-served-provider"] in {"zai", "sglang"}
    if r.headers["x-fi-served-provider"] == "zai":
        assert r.headers["x-fi-served-endpoint"] == f"{CHAT_MODEL}:zai-api"
    else:
        assert "x-fi-served-endpoint" not in r.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity_headers",
    [
        {"x-probe": "synthetic"},
        {"x-fi-served-identity": "include"},
    ],
    ids=["trusted-probe", "authorized-opt-in"],
)
async def test_non_streaming_chat_fallback_reports_the_route_that_served(
    chat_app, identity_headers
):
    """After a fallback, the non-streaming headers must describe the route that
    actually served, and a private host label must still not be published."""

    class _FailingAdapter(BaseAdapter):
        async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
            raise RuntimeError("primary adapter failed")

        async def stream_chat_completion(self, messages, **params):
            raise RuntimeError("primary adapter failed")
            if False:  # pragma: no cover
                yield ""

    primary_cfg = _chat_cfg(CHAT_MODEL)
    primary_cfg.provider = "zai"
    primary_cfg.base_url = "https://example-zai.test"
    primary_cfg.endpoint_id = f"{CHAT_MODEL}:zai-api"
    primary_cfg.endpoint_identity_publishable = True

    fallback_cfg = _chat_cfg(CHAT_MODEL)
    fallback_cfg.provider = "sglang"
    fallback_cfg.base_url = "https://example-sglang.test"
    fallback_cfg.endpoint_id = f"{CHAT_MODEL}:{_PRIVATE_ROUTE_LABEL}"
    fallback_cfg.endpoint_identity_publishable = False

    chat_app.state.services.router.register_route(
        CHAT_MODEL,
        [(_FailingAdapter(primary_cfg), 0.9), (DummyAdapter(fallback_cfg), 0.1)],
    )
    if "x-fi-served-identity" in identity_headers:
        _enable_served_identity_diagnostic(chat_app)
    else:
        _enable_trusted_probe(chat_app)

    transport = ASGITransport(app=chat_app)
    from httpx import AsyncClient as _AC

    async with _AC(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/chat/completions",
            json={"model": CHAT_MODEL, "messages": [{"role": "user", "content": "Hi"}]},
            headers=identity_headers,
        )
    assert r.status_code == 200
    assert r.headers["x-fi-requested-model"] == CHAT_MODEL
    assert r.headers["x-fi-served-model"] == CHAT_MODEL
    # The route that served is the fallback, not the failed primary.
    assert r.headers["x-fi-served-provider"] == "sglang"
    # Its endpoint label is a private host label: it is omitted, not published.
    assert "x-fi-served-endpoint" not in r.headers


# ---------------------------------------------------------------------------
# Header sanitizer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("endpoint_id", "provider", "served_model", "publishable", "expected"),
    [
        ("glm-4.6:local-12003", "sglang", "glm-4.6", True, "glm-4.6:local-12003"),
        ("glm-4.6:zai-api", "zai", "glm-4.6", True, "glm-4.6:zai-api"),
        ("glm-4.6:local", "vllm", "glm-4.6", True, "glm-4.6:local"),
        # LAN octet stamps, hostnames and admin route_ids never reach the
        # header: they are omitted.
        ("glm-4.6:10-api", "zai", "glm-4.6", True, None),
        (f"glm-4.6:{_PRIVATE_ROUTE_LABEL}", "sglang", "glm-4.6", False, None),
        ("glm-4.6:gpu-7-api", "sglang", "glm-4.6", True, None),
        ("glm-4.6:my-internal-box", "zai", "glm-4.6", True, None),
        ("glm-4.6:.local-api", "zai", "glm-4.6", True, None),
        # A trailing newline must not smuggle an unsanitized shape through.
        ("glm-4.6:zai-api\n", "zai", "glm-4.6", True, None),
        # A safe-looking location is not authority for the whole identifier:
        # only a prefix that IS the trusted served model may be republished.
        # The private host label ends in a registry-minted location,
        # but the prefix must come from the registry-resolved served model,
        # not ride along with the location's shape.
        (f"{_PRIVATE_MODEL_LABEL}:local-12003", "zai", "glm-4.6", True, None),
        (
            f"{_PRIVATE_MODEL_LABEL}:local-12003",
            "zai",
            _PRIVATE_MODEL_LABEL,
            True,
            f"{_PRIVATE_MODEL_LABEL}:local-12003",
        ),
        # No trusted served model to anchor the prefix: degrade, fail closed.
        ("glm-4.6:local-12003", "sglang", None, False, None),
        # A colon-less id is not a registry construction at all: the old code
        # fullmatched the whole string as the candidate and published a bare
        # "local"; the anchor check requires the {model}:{location} shape.
        ("local", "vllm", "glm-4.6", True, None),
        (None, "zai", "glm-4.6", True, None),
        (None, None, None, True, None),
        ("", "zai", "glm-4.6", True, None),
    ],
)
def test_header_endpoint_id_sanitization(
    endpoint_id, provider, served_model, publishable, expected
):
    from serving.servers.routers.served_identity import header_endpoint_id

    assert header_endpoint_id(endpoint_id, served_model, publishable=publishable) == expected


def test_served_identity_headers_omit_missing_fields():
    from serving.servers.routers.served_identity import served_identity_headers

    assert served_identity_headers(requested_model="m") == {"X-FI-Requested-Model": "m"}
    headers = served_identity_headers(
        requested_model="auto",
        served_model="glm-4.6",
        endpoint_id="glm-4.6:zai-api",
        provider="zai",
        endpoint_publishable=True,
    )
    assert headers == {
        "X-FI-Requested-Model": "auto",
        "X-FI-Served-Model": "glm-4.6",
        "X-FI-Served-Endpoint": "glm-4.6:zai-api",
        "X-FI-Served-Provider": "zai",
    }


def test_non_latin1_model_names_are_omitted_from_headers():
    from serving.servers.routers.served_identity import served_identity_headers

    assert served_identity_headers(
        requested_model="qwen-通义",
        served_model="qwen-通义",
        endpoint_id="qwen-通义:zai-api",
        provider="zai",
        endpoint_publishable=True,
    ) == {"X-FI-Served-Provider": "zai"}
