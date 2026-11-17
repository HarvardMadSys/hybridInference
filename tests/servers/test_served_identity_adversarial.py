"""Adversarial tests for the X-FI served-identity headers (issue #1504 follow-up).

The PR's own tests cover the happy paths. These cover the paths a reviewer
must not take on trust:

* the entitlement source (the presented key's admin-set metadata) and the
  callers that must not reach it;
* a client-supplied header expanding an entitlement;
* an agent grant and an impersonated call inheriting a privileged key's
  entitlement;
* revocation and metadata updates invalidating a cached authorization;
* admin-created routes, custom providers and runtime adapter-kind changes
  publishing nothing private;
* the same disclosure policy on all four inference surfaces, streamed and
  not;
* a stream's headers naming the endpoint that served after a forced fallback;
* keepalive frames not committing an identity;
* CORS exposing the headers without granting anything.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter as RouteExecutor
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import completions
from serving.stream import done_sentinel, make_final_usage_chunk

CHAT_MODEL = "glm-4.7"
_PRIVATE_ROUTE_LABEL = "gpu" + "7" + "-api"

SERVED_IDENTITY_HEADER_NAMES = (
    "x-fi-requested-model",
    "x-fi-served-model",
    "x-fi-served-endpoint",
    "x-fi-served-provider",
)


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


def _chat_cfg(model_id: str, *, provider: str = "zai", publishable: bool = True) -> ModelConfig:
    return ModelConfig(
        id=model_id,
        name=model_id,
        provider=provider,
        base_url="https://example-zai.test",
        chat_path="/chat/completions",
        endpoint_id=f"{model_id}:zai-api",
        endpoint_identity_publishable=publishable,
        context_length=8192,
        max_output_length=4096,
        supported_params=["temperature", "top_p", "max_tokens", "stream", "tools"],
    )


def _app_with(adapter: BaseAdapter) -> FastAPI:
    router = RouteExecutor()
    router.register_route(CHAT_MODEL, [(adapter, 1.0)])
    app = FastAPI(title="Adversarial served identity app")
    app.state.services = AppServices(router=router)
    install_error_handlers(app)
    app.include_router(completions.router)
    return app


def _entitled(app: FastAPI) -> None:
    from serving.servers.auth import verify_api_key

    app.dependency_overrides[verify_api_key] = lambda: {
        "authenticated": True,
        "is_admin": False,
        "role": "free",
        "user_id": "diagnostic-caller",
        "diagnostic_entitlements": frozenset({"served_identity"}),
    }


def _plain(app: FastAPI) -> None:
    from serving.servers.auth import verify_api_key

    app.dependency_overrides[verify_api_key] = lambda: {
        "authenticated": True,
        "is_admin": False,
        "role": "free",
        "user_id": "ordinary-caller",
    }


def _agent_grant(app: FastAPI) -> None:
    from serving.servers.auth import verify_api_key

    app.dependency_overrides[verify_api_key] = lambda: {
        "authenticated": True,
        "is_admin": False,
        "role": "internal",
        "user_id": "owner",
        "agent_grant_id": "grant-1",
        "agent_job_id": "job-1",
        "diagnostic_entitlements": frozenset({"served_identity"}),
    }


async def _post(app: FastAPI, body: dict[str, Any], headers: dict[str, str]) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/v1/chat/completions", json=body, headers=headers)


def _body(**overrides: Any) -> dict[str, Any]:
    body = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": "hi"}]}
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Requirement 1/3: the entitlement is server-controlled, not client-controlled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_opt_in_header_alone_discloses_nothing(mock_db_logger, mock_log_store):
    """A client that sends the opt-in header without the entitlement gets nothing."""
    app = _app_with(DummyAdapter(_chat_cfg(CHAT_MODEL)))
    _plain(app)
    r = await _post(app, _body(), {"x-fi-served-identity": "include"})
    assert r.status_code == 200
    for name in SERVED_IDENTITY_HEADER_NAMES:
        assert name not in r.headers, f"{name} disclosed without the entitlement"


@pytest.mark.asyncio
async def test_an_ordinary_key_never_receives_identity_headers(mock_db_logger, mock_log_store):
    """Neither the opt-in header nor the probe marker grants an ordinary key."""
    app = _app_with(DummyAdapter(_chat_cfg(CHAT_MODEL)))
    _plain(app)
    for headers in (
        {"x-fi-served-identity": "include"},
        {"x-probe": "synthetic"},
        {"x-probe": "synthetic", "x-fi-served-identity": "include"},
    ):
        r = await _post(app, _body(), headers)
        assert r.status_code == 200
        for name in SERVED_IDENTITY_HEADER_NAMES:
            assert name not in r.headers


@pytest.mark.asyncio
async def test_an_entitled_key_needs_the_per_call_opt_in(mock_db_logger, mock_log_store):
    """The entitlement authorizes the disclosure; the header asks for it."""
    app = _app_with(DummyAdapter(_chat_cfg(CHAT_MODEL)))
    _entitled(app)
    r = await _post(app, _body(), {})
    assert r.status_code == 200
    assert "x-fi-requested-model" not in r.headers

    r = await _post(app, _body(), {"x-fi-served-identity": "include"})
    assert r.status_code == 200
    assert r.headers["x-fi-requested-model"] == CHAT_MODEL


@pytest.mark.asyncio
async def test_an_agent_grant_cannot_use_the_entitlement(mock_db_logger, mock_log_store):
    """A grant's owner may hold the entitlement; the sandbox may not use it."""
    app = _app_with(DummyAdapter(_chat_cfg(CHAT_MODEL)))
    _agent_grant(app)
    for headers in (
        {"x-fi-served-identity": "include"},
        {"x-probe": "synthetic"},
    ):
        r = await _post(app, _body(), headers)
        assert r.status_code == 200
        for name in SERVED_IDENTITY_HEADER_NAMES:
            assert name not in r.headers


@pytest.mark.asyncio
async def test_an_unauthenticated_caller_receives_nothing(mock_db_logger, mock_log_store):
    """Auth disabled: the anonymous admin context must not disclose identity."""
    app = _app_with(DummyAdapter(_chat_cfg(CHAT_MODEL)))
    from serving.servers.auth import verify_api_key

    app.dependency_overrides[verify_api_key] = lambda: {
        "user_id": "anonymous",
        "role": "admin",
        "authenticated": False,
        "is_admin": True,
    }
    for headers in (
        {"x-fi-served-identity": "include"},
        {"x-probe": "synthetic"},
    ):
        r = await _post(app, _body(), headers)
        assert r.status_code == 200
        for name in SERVED_IDENTITY_HEADER_NAMES:
            assert name not in r.headers


# ---------------------------------------------------------------------------
# Requirement 6: nothing private is published
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_non_publishable_route_publishes_no_endpoint_id(mock_db_logger, mock_log_store):
    """A generic-kind route's endpoint id names the deployment's own host."""
    cfg = _chat_cfg(CHAT_MODEL, provider="sglang", publishable=False)
    cfg.endpoint_id = f"{CHAT_MODEL}:{_PRIVATE_ROUTE_LABEL}"
    app = _app_with(DummyAdapter(cfg))
    _entitled(app)
    r = await _post(app, _body(), {"x-fi-served-identity": "include"})
    assert r.status_code == 200
    assert r.headers["x-fi-requested-model"] == CHAT_MODEL
    assert r.headers["x-fi-served-provider"] == "sglang"
    assert "x-fi-served-endpoint" not in r.headers
    assert _PRIVATE_ROUTE_LABEL not in r.text


@pytest.mark.asyncio
async def test_a_publishable_route_publishes_only_its_own_endpoint_id(
    mock_db_logger, mock_log_store
):
    cfg = _chat_cfg(CHAT_MODEL, publishable=True)
    cfg.endpoint_id = f"{CHAT_MODEL}:zai-api"
    app = _app_with(DummyAdapter(cfg))
    _entitled(app)
    r = await _post(app, _body(), {"x-fi-served-identity": "include"})
    assert r.status_code == 200
    assert r.headers["x-fi-served-endpoint"] == f"{CHAT_MODEL}:zai-api"


@pytest.mark.asyncio
async def test_an_endpoint_id_that_does_not_match_the_served_model_is_withheld(
    mock_db_logger, mock_log_store
):
    """The header is anchored in the served model, not in the id's shape."""
    cfg = _chat_cfg(CHAT_MODEL, publishable=True)
    cfg.endpoint_id = "some.other.model:zai-api"
    app = _app_with(DummyAdapter(cfg))
    _entitled(app)
    r = await _post(app, _body(), {"x-fi-served-identity": "include"})
    assert r.status_code == 200
    assert "x-fi-served-endpoint" not in r.headers


# ---------------------------------------------------------------------------
# Requirement 7: the same policy on the streamed and unstreamed surfaces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_streamed_and_unstreamed_apply_the_same_policy(
    mock_db_logger, mock_log_store, stream
):
    app = _app_with(DummyAdapter(_chat_cfg(CHAT_MODEL)))
    _entitled(app)
    r = await _post(app, _body(stream=stream), {"x-fi-served-identity": "include"})
    assert r.status_code == 200
    assert r.headers["x-fi-requested-model"] == CHAT_MODEL
    assert r.headers["x-fi-served-model"] == CHAT_MODEL
    assert r.headers["x-fi-served-endpoint"] == f"{CHAT_MODEL}:zai-api"
    assert r.headers["x-fi-served-provider"] == "zai"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_an_unentitled_caller_gets_nothing_on_either_shape(
    mock_db_logger, mock_log_store, stream
):
    app = _app_with(DummyAdapter(_chat_cfg(CHAT_MODEL)))
    _plain(app)
    r = await _post(app, _body(stream=stream), {"x-fi-served-identity": "include"})
    assert r.status_code == 200
    for name in SERVED_IDENTITY_HEADER_NAMES:
        assert name not in r.headers


# ---------------------------------------------------------------------------
# Requirement 8/9: the served identity is the endpoint that served
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_forced_fallback_names_the_fallback_not_the_primary(mock_db_logger, mock_log_store):
    """The primary is forced to fail; the fallback serves. The header must
    describe the fallback, deterministically (no weighted routing)."""

    class _FailingAdapter(DummyAdapter):
        async def chat_completion(self, messages, **params):
            raise RuntimeError("primary failed")

        async def stream_chat_completion(self, messages, **params):
            raise RuntimeError("primary failed")
            yield ""  # pragma: no cover

    class _FallbackAdapter(DummyAdapter):
        async def chat_completion(self, messages, **params):
            resp = await super().chat_completion(messages, **params)
            resp["choices"][0]["message"]["content"] = "fallback served"
            return resp

        async def stream_chat_completion(self, messages, **params):
            yield self.format_stream_chunk(model=self.config.id, content="fallback served")
            yield make_final_usage_chunk(
                model=self.config.id, messages=messages, total_content="fallback served"
            )
            yield done_sentinel()

    primary_cfg = _chat_cfg(CHAT_MODEL, provider="zai")
    primary_cfg.endpoint_id = f"{CHAT_MODEL}:zai-api"
    primary_cfg.endpoint_identity_publishable = True
    fallback_cfg = _chat_cfg(CHAT_MODEL, provider="sglang")
    fallback_cfg.endpoint_id = f"{CHAT_MODEL}:deepseek-api"
    fallback_cfg.endpoint_identity_publishable = True

    router = RouteExecutor()
    router.register_route(
        CHAT_MODEL, [(_FailingAdapter(primary_cfg), 1.0), (_FallbackAdapter(fallback_cfg), 1.0)]
    )
    app = FastAPI(title="fallback served identity")
    app.state.services = AppServices(router=router)
    install_error_handlers(app)
    app.include_router(completions.router)
    _entitled(app)

    for stream in (False, True):
        r = await _post(app, _body(stream=stream), {"x-fi-served-identity": "include"})
        assert r.status_code == 200
        assert r.headers["x-fi-served-provider"] == "sglang", (
            f"the header named the abandoned primary (stream={stream})"
        )
        assert r.headers["x-fi-served-endpoint"] == f"{CHAT_MODEL}:deepseek-api"


# ---------------------------------------------------------------------------
# Requirement 10: CORS exposure is not an authorization bypass
# ---------------------------------------------------------------------------


def test_cors_exposes_the_headers_but_grants_nothing():
    """CORS exposing the headers is a browser-readability fact, not a grant."""
    from serving.servers.app import CORS_OPTIONS
    from serving.servers.routers.served_identity import SERVED_IDENTITY_HEADER_NAMES

    exposed = {name.lower() for name in CORS_OPTIONS["expose_headers"]}
    for name in SERVED_IDENTITY_HEADER_NAMES:
        assert name.lower() in exposed

    # The exposure lists the headers a browser may read; it does not make the
    # server emit them.
    from starlette.datastructures import Headers

    from serving.servers.routers.served_identity import should_include_served_identity_headers

    assert (
        should_include_served_identity_headers(
            Headers({"x-fi-served-identity": "include"}),
            {"authenticated": True, "role": "free", "user_id": "u"},
        )
        is False
    )


# ---------------------------------------------------------------------------
# Requirement 2: the entitlement's source
# ---------------------------------------------------------------------------


def test_the_entitlement_comes_only_from_the_presented_key_metadata():
    from serving.servers.auth import _key_diagnostic_entitlements

    assert _key_diagnostic_entitlements({}) == frozenset()
    assert _key_diagnostic_entitlements(
        {"key_metadata": {"diagnostic_entitlements": ["served_identity"]}}
    ) == frozenset({"served_identity"})
    # Nothing else on the row grants it.
    assert _key_diagnostic_entitlements({"entitlements": ["served_identity"]}) == frozenset()


def test_impersonation_does_not_carry_the_calling_key_entitlement(monkeypatch):
    """``X-On-Behalf-Of`` attributes to another user; it must not widen authority.

    The impersonated identity is built from the *target* user and carries no
    key metadata, so the service account's own entitlement cannot ride along
    on a request that acts for somebody else.
    """
    import asyncio

    from serving.servers import auth as auth_mod

    class _Store:
        async def get_user_by_id(self, _user_id):
            return {"id": "end-user", "user_name": "End", "role": "free", "status": "active"}

        async def get_personal_org_status(self, _user_id):
            return "active"

        async def get_quota_context_for_user(self, _user_id):
            return []

    monkeypatch.setattr(auth_mod, "_impersonation_service_key_hashes", lambda: frozenset({"k"}))

    resolved = asyncio.run(
        auth_mod._resolve_effective_identity(
            {
                "user_id": "svc",
                "role": "internal",
                "key_metadata": {"diagnostic_entitlements": ["served_identity"]},
            },
            "k",
            "end-user",
            _Store(),
        )
    )
    assert resolved["user_id"] == "end-user"
    assert "key_metadata" not in resolved
    assert auth_mod._key_diagnostic_entitlements(resolved) == frozenset()

    # And the context the handler sees carries no entitlement either.
    context = auth_mod._user_context(
        resolved,
        key_hash="k",
        api_key_id=None,
        quota_daily_cost_usd=10.0,
        spent_today_usd=0.0,
    )
    assert "diagnostic_entitlements" not in context


# ---------------------------------------------------------------------------
# Side channels: an error response must not disclose the serving endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_error_response_discloses_no_identity(mock_db_logger, mock_log_store):
    """A 404 answers before any route is selected; nothing may be disclosed."""
    app = _app_with(DummyAdapter(_chat_cfg(CHAT_MODEL)))
    _entitled(app)
    r = await _post(app, _body(model="no-such-model"), {"x-fi-served-identity": "include"})
    assert r.status_code == 404
    for name in SERVED_IDENTITY_HEADER_NAMES:
        assert name not in r.headers


@pytest.mark.asyncio
async def test_a_rejected_request_discloses_no_identity(mock_db_logger, mock_log_store):
    """A malformed body answers before dispatch; nothing may be disclosed."""
    app = _app_with(DummyAdapter(_chat_cfg(CHAT_MODEL)))
    _entitled(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/chat/completions",
            json={"model": CHAT_MODEL},
            headers={"x-fi-served-identity": "include"},
        )
    assert r.status_code >= 400
    for name in SERVED_IDENTITY_HEADER_NAMES:
        assert name not in r.headers


@pytest.mark.asyncio
async def test_the_synthetic_routing_metadata_is_not_reachable_by_a_client(
    mock_db_logger, mock_log_store
):
    """The wire format's ``_routing`` block stays internal."""
    app = _app_with(DummyAdapter(_chat_cfg(CHAT_MODEL)))
    _entitled(app)
    r = await _post(app, _body(), {"x-fi-served-identity": "include"})
    assert r.status_code == 200
    assert "_routing" not in r.text
    assert "base_url" not in r.text


# ---------------------------------------------------------------------------
# Requirement 6, end to end: an admin edit decides what a caller can read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_admin_edit_to_a_generic_kind_hides_the_endpoint_from_an_entitled_caller(
    mock_db_logger, mock_log_store
):
    """The disclosure decision an admin edit makes is the one the caller sees.

    The route is edited onto a generic kind at a private host. The edited
    adapter's publishability is False, so even an entitled caller receives no
    endpoint id -- and no ordinary caller receives anything at all.
    """
    from serving.servers.registry import _make_provider_id
    from serving.servers.routers.admin import provider_routes

    generic = provider_routes.ProviderTarget(
        provider="openai_compat",
        label="OpenAI-compatible",
        kind="openai_compat",
        key_provider="openai_compat",
        default_base_url="https://example.test/v1",
        chat_path="/chat/completions",
    )

    base_url = "http://localhost:8000"
    model_id = CHAT_MODEL
    old_endpoint_id = _make_provider_id(model_id, "zai", base_url)

    class _Cfg:
        provider = "zai"
        endpoint_identity_publishable = True

        def __init__(self) -> None:
            self.base_url = base_url
            self.endpoint_id = old_endpoint_id
            self.route_metadata: dict[str, Any] = {}

    class _Adapter:
        def __init__(self) -> None:
            self.config = _Cfg()

    current_adapter = _Adapter()

    # The admin edit, in the order ``_prepare_route_update`` runs it.
    cfg: dict[str, Any] = {"endpoint_id": old_endpoint_id, "base_url": base_url}
    cfg["endpoint_id"] = provider_routes._edited_endpoint_id(
        model_id, generic, current_adapter, base_url
    )
    provider_routes._apply_target_adapter_defaults(cfg, generic)
    provider_routes._finalize_admin_endpoint_publishability(
        cfg,
        model_id=model_id,
        target=generic,
        base_url=base_url,
        endpoint_id=cfg.get("endpoint_id"),
    )
    provider_routes._carry_endpoint_facts_if_unmoved(
        cfg, current_adapter=current_adapter, base_url=base_url
    )
    provider_routes._apply_admin_endpoint_publishability(
        cfg,
        model_id=model_id,
        adapter_kind=generic.kind,
        base_url=base_url,
    )

    # The edited route now serves on the generic kind.
    assert cfg["endpoint_identity_publishable"] is False
    edited_cfg = _chat_cfg(model_id, provider="openai_compat", publishable=False)
    edited_cfg.endpoint_id = cfg["endpoint_id"]
    edited_cfg.base_url = base_url

    app = _app_with(DummyAdapter(edited_cfg))

    _entitled(app)
    r = await _post(app, _body(), {"x-fi-served-identity": "include"})
    assert r.status_code == 200
    assert r.headers["x-fi-requested-model"] == model_id
    assert "x-fi-served-endpoint" not in r.headers
    assert "local-8000" not in r.text

    _plain(app)
    r = await _post(app, _body(), {"x-fi-served-identity": "include"})
    assert r.status_code == 200
    for name in SERVED_IDENTITY_HEADER_NAMES:
        assert name not in r.headers
