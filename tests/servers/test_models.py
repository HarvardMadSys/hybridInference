from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers import registry
from serving.servers.auth import optional_verify_api_key
from serving.servers.deps import AppServices, get_current_user
from serving.servers.routers import models, user_routes

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class _Adapter(BaseAdapter):
    def __init__(self, cfg: ModelConfig, content: str = "ok"):
        super().__init__(cfg)
        self._content = content

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content=self._content, model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ):  # pragma: no cover
        yield self.format_stream_chunk(model=self.config.id, content=self._content)


class _VisibilityResolver:
    def __init__(self, overrides: dict[str, str]):
        self._overrides = overrides

    async def get_effective_required_role(self, model_id: str, default_role: str) -> str:
        return self._overrides.get(model_id, default_role)


def _cfg(
    *,
    id: str,
    provider: str = "prov",
    context: int = 8192,
    max_out: int = 4096,
    supported: list[str] | None = None,
    tools: bool = False,
    structured: bool = False,
) -> ModelConfig:
    return ModelConfig(
        id=id,
        name=id,
        provider=provider,
        base_url="http://test",
        context_length=context,
        max_output_length=max_out,
        supported_params=supported or ["temperature", "top_p", "max_tokens"],
        supports_tools=tools,
        supports_structured_output=structured,
        input_modalities=["text"],
        output_modalities=["text"],
        quantization="bf16",
    )


@pytest.fixture
async def models_app() -> FastAPI:
    router = RouteExecutor()
    # Two adapters to exercise aggregation
    a1 = _Adapter(
        _cfg(
            id="canonical-model",
            provider="provA",
            context=8192,
            max_out=4096,
            supported=["temperature", "top_p", "max_tokens", "seed"],
            tools=True,
            structured=False,
        )
    )
    a2 = _Adapter(
        _cfg(
            id="canonical-model",
            provider="provB",
            context=4096,
            max_out=2048,
            supported=["temperature", "max_tokens"],
            tools=False,
            structured=True,
        )
    )
    router.register_route("alias-model", [(a1, 0.5), (a2, 0.5)])
    router.register_route("canonical-model", [(a1, 1.0)])

    app = FastAPI(title="Models App")
    app.state.services = AppServices(router=router, db_logger=None)  # type: ignore[attr-defined]
    app.include_router(models.router)
    return app


@pytest.fixture
async def models_client(models_app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=models_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_models_aggregation_and_slug(models_client: AsyncClient):
    resp = await models_client.get("/v1/models")
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()["data"]
    items = [m for m in data if m["id"] == "canonical-model"]
    assert len(items) == 1
    item = items[0]
    assert item["context_length"] == 4096
    assert item["max_output_length"] == 2048
    assert item["supported_sampling_parameters"] == ["max_tokens", "temperature"]
    assert "tools" in item["supported_features"]
    assert "json_mode" in item["supported_features"]
    assert "structured_outputs" in item["supported_features"]
    assert item.get("openrouter", {}).get("slug") in {"alias-model", None}


@pytest.mark.asyncio
async def test_models_omit_unpublished_routes():
    router = RouteExecutor()
    router.register_route("visible-model", [(_Adapter(_cfg(id="visible-model")), 1.0)])
    router.register_route(
        "staged-model",
        [(_Adapter(_cfg(id="staged-model")), 1.0)],
        published=False,
    )
    app = FastAPI()
    app.state.services = AppServices(router=router, db_logger=None)  # type: ignore[attr-defined]
    app.include_router(models.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/models")

    assert resp.status_code == status.HTTP_200_OK
    model_ids = {model["id"] for model in resp.json()["data"]}
    assert "visible-model" in model_ids
    assert "staged-model" not in model_ids


@pytest.mark.asyncio
async def test_models_empty_routes_returns_empty_list():
    router = RouteExecutor()
    app = FastAPI()
    app.state.services = AppServices(router=router, db_logger=None)  # type: ignore[attr-defined]
    app.include_router(models.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/models")
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["data"] == []


@pytest.mark.asyncio
async def test_models_single_adapter_no_aggregation():
    router = RouteExecutor()
    a = _Adapter(_cfg(id="solo", context=1234, max_out=321, supported=["temperature"], tools=True))
    router.register_route("solo", [(a, 1.0)])
    app = FastAPI()
    app.state.services = AppServices(router=router, db_logger=None)  # type: ignore[attr-defined]
    app.include_router(models.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/models")
        data = resp.json()["data"]
        item = next(m for m in data if m["id"] == "solo")
        assert item["context_length"] == 1234
        assert item["max_output_length"] == 321
        assert item["supported_sampling_parameters"] == ["temperature"]
        assert "tools" in item["supported_features"]


@pytest.mark.asyncio
async def test_models_openai_provider_is_preserved_for_gpt_style_entry(tmp_path, monkeypatch):
    from serving.servers.auth import optional_verify_api_key

    yaml_text = (
        "models:\n"
        "  - id: gpt-5.5\n"
        "    name: GPT-5.5\n"
        "    provider: openai\n"
        "    required_role: internal\n"
        "    provider_model_id: gpt-5.5\n"
        "    quantization: none\n"
        "    context_length: 1050000\n"
        "    max_output_length: 128000\n"
        "    supports_tools: true\n"
        "    supports_structured_output: true\n"
        "    supported_params: [max_tokens, stream, tools, tool_choice, reasoning_effort]\n"
        "    input_modalities: [text, image]\n"
        "    output_modalities: [text]\n"
        "    pricing:\n"
        "      prompt: '5.00'\n"
        "      completion: '30.00'\n"
        "      image: '0'\n"
        "      request: '0'\n"
        "      input_cache_reads: '5.00'\n"
        "      input_cache_writes: '0.00'\n"
        "    route:\n"
        "      - kind: openai_compat\n"
        "        weight: 1.0\n"
        "        base_url: ${CLI_PROXY_BASE_URL}\n"
        "        api_key: ${CLI_PROXY_API_KEY}\n"
        "        provider_model_id: gpt-5.5\n"
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)
    monkeypatch.setenv("CLI_PROXY_BASE_URL", "http://cliproxy.local/v1")
    monkeypatch.setenv("CLI_PROXY_API_KEY", "sk-test")

    router = RouteExecutor()
    registry.register_from_models_yaml(router, p)

    app = FastAPI()
    app.state.services = AppServices(router=router, db_logger=None)  # type: ignore[attr-defined]
    app.dependency_overrides[optional_verify_api_key] = lambda: {
        "is_admin": True,
        "role": "admin",
        "user_id": "admin-user",
    }
    app.include_router(models.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/models")

    item = next(m for m in resp.json()["data"] if m["id"] == "gpt-5.5")
    assert item["owned_by"] == "openai"


@pytest.mark.asyncio
async def test_models_pricing_primary_config_behavior():
    router = RouteExecutor()
    p_primary = {"prompt": "1", "completion": "2"}
    p_secondary = {"prompt": "9", "completion": "9"}
    a1 = _Adapter(_cfg(id="price", provider="p1"))
    a1.config.pricing = dict(p_primary)
    a2 = _Adapter(_cfg(id="price", provider="p2"))
    a2.config.pricing = dict(p_secondary)
    router.register_route("price", [(a1, 0.9), (a2, 0.1)])
    app = FastAPI()
    app.state.services = AppServices(router=router, db_logger=None)  # type: ignore[attr-defined]
    app.include_router(models.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/models")
        data = resp.json()["data"]
        item = next(m for m in data if m["id"] == "price")
        assert item["pricing"] == p_primary


@pytest.mark.asyncio
async def test_concurrent_model_requests_consistent(models_client: AsyncClient):
    tasks = [models_client.get("/v1/models") for _ in range(10)]
    results = await asyncio.gather(*tasks)
    first = results[0].json()
    assert all(r.json() == first for r in results)


@pytest.mark.asyncio
@pytest.mark.perf
@pytest.mark.skipif(
    os.getenv("RUN_PERF") != "1",
    reason="Performance tests are disabled by default (set RUN_PERF=1 to enable)",
)
async def test_models_endpoint_performance(models_client: AsyncClient):
    start = time.perf_counter()
    resp = await models_client.get("/v1/models")
    elapsed = time.perf_counter() - start
    assert resp.status_code == status.HTTP_200_OK
    assert elapsed < 0.2


# ---------------------------------------------------------------------------
# admin_only visibility tests
# ---------------------------------------------------------------------------


def _build_admin_app(user_ctx: dict | None) -> FastAPI:
    """Build a test app with one admin_only model and inject user_ctx."""
    from serving.servers.auth import optional_verify_api_key

    router_exec = RouteExecutor()
    public = _Adapter(_cfg(id="public-model"))
    secret = _Adapter(_cfg(id="secret-model"))
    router_exec.register_route("public-model", [(public, 1.0)])
    router_exec.register_route("secret-model", [(secret, 1.0)], admin_only=True)

    app = FastAPI()
    app.state.services = AppServices(router=router_exec, db_logger=None)  # type: ignore[attr-defined]

    # Override the optional_verify_api_key dependency to return our test value
    app.dependency_overrides[optional_verify_api_key] = lambda: user_ctx
    app.include_router(models.router)
    return app


@pytest.mark.asyncio
async def test_admin_only_hidden_no_auth():
    """Admin-only route not listed when user_ctx is None (unauthenticated)."""
    app = _build_admin_app(user_ctx=None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/models")
        assert resp.status_code == status.HTTP_200_OK
        ids = [m["id"] for m in resp.json()["data"]]
        assert "public-model" in ids
        assert "secret-model" not in ids


@pytest.mark.asyncio
async def test_admin_only_visible_to_admin():
    """Admin-only route listed when user_ctx has is_admin=True."""
    app = _build_admin_app(user_ctx={"is_admin": True, "role": "admin", "user_id": "admin-user"})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/models")
        assert resp.status_code == status.HTTP_200_OK
        ids = [m["id"] for m in resp.json()["data"]]
        assert "public-model" in ids
        assert "secret-model" in ids


@pytest.mark.asyncio
async def test_models_runtime_visibility_override_hides_model_for_free_user():
    router = RouteExecutor()
    visible = _Adapter(_cfg(id="visible-model"))
    hidden = _Adapter(_cfg(id="runtime-hidden-model"))
    router.register_route("visible-model", [(visible, 1.0)])
    router.register_route("runtime-hidden-model", [(hidden, 1.0)])

    app = FastAPI()
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        db_logger=None,
        model_visibility_resolver=_VisibilityResolver({"runtime-hidden-model": "admin"}),
    )
    app.dependency_overrides[optional_verify_api_key] = lambda: {
        "authenticated": True,
        "role": "free",
        "user_id": "free-user",
    }
    app.include_router(models.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/models")

    assert resp.status_code == status.HTTP_200_OK
    ids = [m["id"] for m in resp.json()["data"]]
    assert "visible-model" in ids
    assert "runtime-hidden-model" not in ids


@pytest.mark.asyncio
async def test_models_runtime_visibility_override_uses_canonical_model_id_for_aliases():
    router = RouteExecutor()
    aliased = _Adapter(_cfg(id="canonical-model"))
    router.register_route("alias-model", [(aliased, 1.0)], aliases=["secondary-alias"])

    app = FastAPI()
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        db_logger=None,
        model_visibility_resolver=_VisibilityResolver({"canonical-model": "admin"}),
    )
    app.dependency_overrides[optional_verify_api_key] = lambda: {
        "authenticated": True,
        "role": "free",
        "user_id": "free-user",
    }
    app.include_router(models.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/models")

    assert resp.status_code == status.HTTP_200_OK
    ids = [m["id"] for m in resp.json()["data"]]
    assert "canonical-model" not in ids


@pytest.mark.asyncio
async def test_user_models_runtime_visibility_override_hides_model_for_free_user():
    router = RouteExecutor()
    visible = _Adapter(_cfg(id="visible-model"))
    hidden = _Adapter(_cfg(id="runtime-hidden-model"))
    router.register_route("visible-model", [(visible, 1.0)])
    router.register_route("runtime-hidden-model", [(hidden, 1.0)])

    app = FastAPI()
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        db_logger=None,
        model_visibility_resolver=_VisibilityResolver({"runtime-hidden-model": "admin"}),
    )
    app.dependency_overrides[get_current_user] = lambda: {
        "authenticated": True,
        "role": "free",
        "user_id": "free-user",
    }
    app.include_router(user_routes.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/user/models")

    assert resp.status_code == status.HTTP_200_OK
    ids = [m["id"] for m in resp.json()["data"]]
    assert "visible-model" in ids
    assert "runtime-hidden-model" not in ids


@pytest.mark.asyncio
async def test_user_models_excludes_disabled_models_for_user():
    router = RouteExecutor()
    visible = _Adapter(_cfg(id="visible-model"))
    disabled = _Adapter(_cfg(id="disabled-model"))
    router.register_route("visible-model", [(visible, 1.0)])
    router.register_route("disabled-alias", [(disabled, 1.0)])

    app = FastAPI()
    app.state.services = AppServices(router=router, db_logger=None)  # type: ignore[attr-defined]
    app.dependency_overrides[get_current_user] = lambda: {
        "authenticated": True,
        "role": "free",
        "user_id": "free-user",
    }

    async def _get_op_store():
        store = type("Store", (), {})()
        store.get_user_preferences = AsyncMock(return_value={"disabled_models": ["disabled-model"]})
        return store

    from serving.servers.deps import get_operational_store

    app.dependency_overrides[get_operational_store] = _get_op_store
    app.include_router(user_routes.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/user/models")

    assert resp.status_code == status.HTTP_200_OK
    ids = [m["id"] for m in resp.json()["data"]]
    assert "visible-model" in ids
    assert "disabled-model" not in ids


@pytest.mark.asyncio
async def test_trial_user_sees_same_models_as_free():
    """Trial-role users must see the same model set as free-role users."""
    from serving.servers.auth import optional_verify_api_key

    router_exec = RouteExecutor()
    public = _Adapter(_cfg(id="public-model"))
    router_exec.register_route("public-model", [(public, 1.0)])

    def _make_app(role: str) -> FastAPI:
        app = FastAPI()
        app.state.services = AppServices(router=router_exec, db_logger=None)  # type: ignore[attr-defined]
        app.dependency_overrides[optional_verify_api_key] = lambda: {"role": role}
        app.include_router(models.router)
        return app

    transport_free = ASGITransport(_make_app("free"))
    transport_trial = ASGITransport(_make_app("trial"))

    async with AsyncClient(transport=transport_free, base_url="http://test") as c:
        free_ids = [m["id"] for m in (await c.get("/v1/models")).json()["data"]]
    async with AsyncClient(transport=transport_trial, base_url="http://test") as c:
        trial_ids = [m["id"] for m in (await c.get("/v1/models")).json()["data"]]

    assert free_ids == trial_ids
    assert "public-model" in trial_ids
