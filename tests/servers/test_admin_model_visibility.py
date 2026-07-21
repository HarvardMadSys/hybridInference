"""Tests for admin model visibility endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.config.model_visibility import ModelVisibilityResolver
from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router


def _model_by_id(models: list[dict], model_id: str) -> dict:
    return next(model for model in models if model["model_id"] == model_id)


@pytest.fixture
async def admin_client(monkeypatch):
    op_store = MagicMock()
    op_store.list_model_visibility_overrides = AsyncMock(return_value=[])
    op_store.get_model_visibility_override = AsyncMock(return_value=None)
    op_store.set_model_visibility_override = AsyncMock()
    op_store.delete_model_visibility_override = AsyncMock()

    router = RouteExecutor()
    adapter = MagicMock()
    adapter.config.id = "public-model"
    adapter.config.name = "Public Model"
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
    router.register_route(
        "public-model",
        [(adapter, 1.0)],
        aliases=["public-model-alias"],
        required_role="free",
    )
    provider_model_adapter = MagicMock()
    provider_model_adapter.config.id = "provider/model"
    provider_model_adapter.config.name = "Provider Model"
    provider_model_adapter.config.provider = "test"
    provider_model_adapter.config.context_length = 8192
    provider_model_adapter.config.max_output_length = 4096
    provider_model_adapter.config.supported_params = []
    provider_model_adapter.config.supports_tools = False
    provider_model_adapter.config.supports_structured_output = False
    provider_model_adapter.config.input_modalities = ["text"]
    provider_model_adapter.config.output_modalities = ["text"]
    provider_model_adapter.config.quantization = None
    provider_model_adapter.config.pricing = None
    router.register_route(
        "provider/model",
        [(provider_model_adapter, 1.0)],
        required_role="free",
    )

    app = FastAPI()
    app.state.services = AppServices(
        router=router,
        operational_store=op_store,
        model_visibility_resolver=ModelVisibilityResolver(op_store),
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )
    app.include_router(admin_router.router)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    monkeypatch.setattr(
        "serving.servers.routers.admin.model_visibility.log_admin_action",
        AsyncMock(),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.app = app  # type: ignore[attr-defined]
        yield client, op_store


@pytest.mark.asyncio
async def test_list_model_visibility_returns_baseline_and_effective_roles(admin_client):
    client, _ = admin_client

    response = await client.get(
        "/admin/models/visibility",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    data = response.json()
    model = _model_by_id(data["models"], "public-model")
    assert model["model_id"] == "public-model"
    assert model["baseline_required_role"] == "free"
    assert model["override_required_role"] is None
    assert model["effective_required_role"] == "free"


@pytest.mark.asyncio
async def test_patch_model_visibility_sets_override(admin_client):
    client, op_store = admin_client

    response = await client.patch(
        "/admin/models/public-model/visibility",
        json={"required_role": "admin"},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    op_store.set_model_visibility_override.assert_awaited_once_with(
        "public-model", "admin", "127.0.0.1"
    )


@pytest.mark.asyncio
async def test_patch_model_visibility_supports_slash_model_ids(admin_client):
    client, op_store = admin_client

    response = await client.patch(
        "/admin/models/provider/model/visibility",
        json={"required_role": "admin"},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    op_store.set_model_visibility_override.assert_awaited_once_with(
        "provider/model", "admin", "127.0.0.1"
    )


@pytest.mark.asyncio
async def test_patch_model_visibility_on_alias_returns_404(admin_client):
    client, _ = admin_client

    response = await client.patch(
        "/admin/models/public-model-alias/visibility",
        json={"required_role": "admin"},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_patch_model_visibility_with_null_clears_override(admin_client):
    client, op_store = admin_client

    response = await client.patch(
        "/admin/models/public-model/visibility",
        json={"required_role": None},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    op_store.delete_model_visibility_override.assert_awaited_once_with("public-model")


@pytest.mark.asyncio
async def test_list_model_visibility_reflects_override_after_patch(admin_client):
    client, op_store = admin_client

    patch_response = await client.patch(
        "/admin/models/public-model/visibility",
        json={"required_role": "admin"},
        headers={"Authorization": "Bearer test-admin"},
    )
    assert patch_response.status_code == 200

    op_store.list_model_visibility_overrides.return_value = [
        {"model_id": "public-model", "required_role": "admin"}
    ]
    op_store.get_model_visibility_override.return_value = {
        "model_id": "public-model",
        "required_role": "admin",
    }

    response = await client.get(
        "/admin/models/visibility",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    data = response.json()
    model = _model_by_id(data["models"], "public-model")
    assert model["override_required_role"] == "admin"
    assert model["effective_required_role"] == "admin"


@pytest.mark.asyncio
async def test_list_model_visibility_invalid_override_fails_closed_to_admin(admin_client):
    client, op_store = admin_client
    op_store.list_model_visibility_overrides.return_value = [
        {"model_id": "public-model", "required_role": "not-a-role"}
    ]
    op_store.get_model_visibility_override.return_value = {
        "model_id": "public-model",
        "required_role": "not-a-role",
    }

    response = await client.get(
        "/admin/models/visibility",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    data = response.json()
    model = _model_by_id(data["models"], "public-model")
    assert model["override_required_role"] == "not-a-role"
    assert model["effective_required_role"] == "admin"


@pytest.mark.asyncio
async def test_ambiguous_visibility_write_fences_cached_role(admin_client):
    client, op_store = admin_client
    resolver = client.app.state.services.model_visibility_resolver
    op_store.get_model_visibility_override.return_value = {
        "model_id": "public-model",
        "required_role": "free",
    }
    assert await resolver.get_effective_required_role("public-model", "admin") == "free"
    op_store.get_model_visibility_override.return_value = {
        "model_id": "public-model",
        "required_role": "admin",
    }
    op_store.set_model_visibility_override.side_effect = RuntimeError(
        "connection dropped after commit"
    )

    with pytest.raises(RuntimeError, match="connection dropped after commit"):
        await client.patch(
            "/admin/models/public-model/visibility",
            json={"required_role": "admin"},
            headers={"Authorization": "Bearer test-admin"},
        )

    assert await resolver.get_effective_required_role("public-model", "admin") == "admin"
