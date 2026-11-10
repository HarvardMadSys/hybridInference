"""An admin can disable an embedding model for a user, end to end.

The model visibility catalog lists embedding models next to chat models, and
the admin UI offers every listed model as a per-user denylist choice. The
denylist PATCH must therefore keep a listed embedding model's canonical id
instead of dropping it as unknown, and /v1/embeddings must refuse the model to
that user from the stored preference. Aliases and unknown names are still
dropped: the denylist is keyed by canonical id.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.config.model_visibility import ModelVisibilityResolver
from serving.config.runtime_settings import get_runtime_settings
from serving.model_access import get_disabled_models_from_preferences
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import (
    AppServices,
    get_completions_logger,
    get_embedding_adapters,
    get_log_store,
    get_model_visibility_resolver,
    get_operational_store,
    get_router,
)
from serving.servers.routers import admin as admin_router, embeddings

ADMIN_AUTH = {"Authorization": "Bearer test-admin"}


class _EmbeddingAdapter:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            id="emb-x", provider="fake-provider", pricing={"prompt": "1", "completion": "0"}
        )
        self.calls = 0

    async def embeddings(self, input_data, **params):
        self.calls += 1
        return {
            "object": "list",
            "model": "emb-x",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }


class _NullLogger:
    def schedule_log(self, request_id: str, log_data: dict[str, Any]) -> None:
        pass


class _FakeRuntimeSettings:
    async def get_bool(self, key: str) -> bool:
        return False


def _shared_router(embedding_adapter: _EmbeddingAdapter) -> FixedRouter:
    router = FixedRouter()
    chat_adapter = MagicMock()
    chat_adapter.config.id = "chat-model"
    router.register_route("chat-model", [(chat_adapter, 1.0)], required_role="free")
    router.register_embedding_route(
        "emb-x", [(embedding_adapter, 1.0)], aliases=["emb-x-alias"], required_role="free"
    )
    return router


def _admin_app(router: FixedRouter, op_store) -> FastAPI:
    app = FastAPI()
    app.state.services = AppServices(
        router=router,
        operational_store=op_store,
        model_visibility_resolver=ModelVisibilityResolver(op_store),
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )
    app.include_router(admin_router.router)
    return app


def _embeddings_app(router: FixedRouter, adapter: _EmbeddingAdapter, user_ctx: dict) -> FastAPI:
    app = FastAPI()
    app.include_router(embeddings.router)
    app.dependency_overrides[verify_api_key] = lambda: user_ctx
    app.dependency_overrides[enforce_user_concurrency] = lambda: None
    app.dependency_overrides[get_router] = lambda: router
    app.dependency_overrides[get_embedding_adapters] = lambda: {
        "emb-x": adapter,
        "emb-x-alias": adapter,
    }
    app.dependency_overrides[get_model_visibility_resolver] = lambda: None
    app.dependency_overrides[get_log_store] = lambda: object()
    app.dependency_overrides[get_completions_logger] = lambda: _NullLogger()
    app.dependency_overrides[get_operational_store] = lambda: None
    app.dependency_overrides[get_runtime_settings] = lambda: _FakeRuntimeSettings()
    return app


async def _post_embedding(app: FastAPI, model: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/v1/embeddings", json={"model": model, "input": "hi"})
    await asyncio.sleep(0.05)
    return resp


@pytest.mark.asyncio
async def test_admin_denylist_keeps_a_listed_embedding_model_and_the_gate_refuses_it(
    monkeypatch,
):
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    monkeypatch.setattr("serving.servers.routers.admin.users.log_admin_action", AsyncMock())

    preferences: dict[str, Any] = {}

    async def _set_user_preference(user_id: str, key: str, value: Any) -> bool:
        preferences[key] = value
        return True

    op_store = MagicMock()
    op_store.list_model_visibility_overrides = AsyncMock(return_value=[])
    op_store.get_user_by_id = AsyncMock(
        return_value={"id": "u1", "email": "u1@example.com", "status": "active", "role": "pro"}
    )
    op_store.set_user_preference = AsyncMock(side_effect=_set_user_preference)

    adapter = _EmbeddingAdapter()
    router = _shared_router(adapter)
    admin_app = _admin_app(router, op_store)

    async with AsyncClient(transport=ASGITransport(app=admin_app), base_url="http://test") as c:
        listing = await c.get("/admin/models/visibility", headers=ADMIN_AUTH)
        assert listing.status_code == 200
        listed = [item["model_id"] for item in listing.json()["models"]]
        # The catalog the admin UI picks from lists the embedding model by its
        # canonical id only.
        assert "emb-x" in listed
        assert "emb-x-alias" not in listed

        patched = await c.patch(
            "/admin/users/u1",
            headers=ADMIN_AUTH,
            json={"disabled_models": [*listed, "emb-x-alias", "retired-model"]},
        )

    assert patched.status_code == 200
    assert patched.json()["updated_fields"] == ["disabled_models"]
    # Every listed model is kept, embedding included; the alias and the
    # unknown name are dropped.
    assert preferences["disabled_models"] == ["chat-model", "emb-x"]

    # The stored denylist, read the way the auth dependency reads it, refuses
    # the embedding model by canonical id and by alias without dispatching.
    denied_ctx = {
        "user_id": "u1",
        "role": "pro",
        "authenticated": True,
        "disabled_models": get_disabled_models_from_preferences(preferences),
    }
    denied_app = _embeddings_app(router, adapter, denied_ctx)
    for model in ("emb-x", "emb-x-alias"):
        resp = await _post_embedding(denied_app, model)
        assert resp.status_code == 404, model
    assert adapter.calls == 0

    # Control: the same model is served to a user who has not disabled it.
    allowed_ctx = {"user_id": "u2", "role": "pro", "authenticated": True}
    resp = await _post_embedding(_embeddings_app(router, adapter, allowed_ctx), "emb-x")
    assert resp.status_code == 200
    assert adapter.calls == 1
