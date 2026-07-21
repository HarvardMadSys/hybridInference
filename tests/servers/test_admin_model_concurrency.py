"""Tests for admin model concurrency-exemption endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.config.model_concurrency import ModelConcurrencyResolver
from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router


def _model_by_id(models: list[dict], model_id: str) -> dict:
    return next(model for model in models if model["model_id"] == model_id)


def _make_adapter(model_id: str, name: str):
    adapter = MagicMock()
    adapter.config.id = model_id
    adapter.config.name = name
    adapter.config.provider = "test"
    adapter.config.context_length = 8192
    adapter.config.max_output_length = 4096
    adapter.config.supported_params = []
    adapter.config.supports_tools = False
    adapter.config.supports_structured_output = False
    adapter.config.input_modalities = ["text"]
    adapter.config.output_modalities = ["text"]
    adapter.config.quantization = None
    adapter.config.pricing = None
    return adapter


@pytest.fixture
async def admin_client(monkeypatch):
    op_store = MagicMock()
    op_store.list_model_concurrency_exemptions = AsyncMock(return_value=[])
    op_store.get_model_concurrency_exemption = AsyncMock(return_value=None)
    op_store.set_model_concurrency_exemption = AsyncMock()
    op_store.delete_model_concurrency_exemption = AsyncMock()

    router = RouteExecutor()
    router.register_route(
        "public-model",
        [(_make_adapter("public-model", "Public Model"), 1.0)],
        aliases=["public-model-alias"],
        required_role="free",
    )
    router.register_route(
        "provider/model",
        [(_make_adapter("provider/model", "Provider Model"), 1.0)],
        required_role="free",
    )

    app = FastAPI()
    app.state.services = AppServices(
        router=router,
        operational_store=op_store,
        model_concurrency_resolver=ModelConcurrencyResolver(op_store),
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )
    app.include_router(admin_router.router)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    monkeypatch.setattr(
        "serving.servers.routers.admin.model_concurrency.log_admin_action",
        AsyncMock(),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.app = app  # type: ignore[attr-defined]
        yield client, op_store


@pytest.mark.asyncio
async def test_list_model_concurrency_defaults_to_not_exempt(admin_client):
    client, _ = admin_client

    response = await client.get(
        "/admin/models/concurrency",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    data = response.json()
    model = _model_by_id(data["models"], "public-model")
    assert model["model_id"] == "public-model"
    assert model["exempt"] is False


@pytest.mark.asyncio
async def test_patch_model_concurrency_sets_exemption(admin_client):
    client, op_store = admin_client

    response = await client.patch(
        "/admin/models/public-model/concurrency",
        json={"exempt": True},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    assert response.json()["exempt"] is True
    op_store.set_model_concurrency_exemption.assert_awaited_once_with("public-model", "127.0.0.1")


@pytest.mark.asyncio
async def test_patch_model_concurrency_supports_slash_model_ids(admin_client):
    client, op_store = admin_client

    response = await client.patch(
        "/admin/models/provider/model/concurrency",
        json={"exempt": True},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    op_store.set_model_concurrency_exemption.assert_awaited_once_with("provider/model", "127.0.0.1")


@pytest.mark.asyncio
async def test_patch_model_concurrency_on_alias_returns_404(admin_client):
    client, _ = admin_client

    response = await client.patch(
        "/admin/models/public-model-alias/concurrency",
        json={"exempt": True},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_patch_model_concurrency_false_clears_exemption(admin_client):
    client, op_store = admin_client

    response = await client.patch(
        "/admin/models/public-model/concurrency",
        json={"exempt": False},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    assert response.json()["exempt"] is False
    op_store.delete_model_concurrency_exemption.assert_awaited_once_with("public-model")


@pytest.mark.asyncio
async def test_list_model_concurrency_reflects_exemption(admin_client):
    client, op_store = admin_client
    op_store.list_model_concurrency_exemptions.return_value = [{"model_id": "public-model"}]

    response = await client.get(
        "/admin/models/concurrency",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    data = response.json()
    model = _model_by_id(data["models"], "public-model")
    assert model["exempt"] is True


@pytest.mark.asyncio
async def test_ambiguous_concurrency_write_fences_cached_exemption(admin_client):
    client, op_store = admin_client
    resolver = client.app.state.services.model_concurrency_resolver
    op_store.get_model_concurrency_exemption.return_value = {"model_id": "public-model"}
    assert await resolver.is_exempt("public-model") is True
    op_store.get_model_concurrency_exemption.return_value = None
    op_store.set_model_concurrency_exemption.side_effect = RuntimeError(
        "connection dropped after commit"
    )

    with pytest.raises(RuntimeError, match="connection dropped after commit"):
        await client.patch(
            "/admin/models/public-model/concurrency",
            json={"exempt": True},
            headers={"Authorization": "Bearer test-admin"},
        )

    assert await resolver.is_exempt("public-model") is False
