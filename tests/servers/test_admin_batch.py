"""Admin endpoints for the per-model batch concurrency caps."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.batch import config as batch_config_module
from serving.batch.model_limits import BatchModelLimits
from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router

ADMIN = {"Authorization": "Bearer test-admin"}


class _FakeBatchStore:
    """The ``BatchStore`` methods the admin endpoints call, over dicts."""

    def __init__(self) -> None:
        self.limits: dict[str, int] = {}
        self.stats: dict[str, dict[str, int]] = {}
        self.writes: list[tuple] = []
        #: Raised after a write is applied: a commit whose reply was lost.
        self.write_error: Exception | None = None

    async def list_model_limits(self) -> dict[str, int]:
        return dict(self.limits)

    async def set_model_limit(
        self, model_id: str, max_concurrency: int, updated_by: str | None
    ) -> None:
        self.writes.append(("set", model_id, max_concurrency, updated_by))
        self.limits[model_id] = max_concurrency
        if self.write_error is not None:
            raise self.write_error

    async def delete_model_limit(self, model_id: str) -> bool:
        self.writes.append(("delete", model_id))
        existed = self.limits.pop(model_id, None) is not None
        if self.write_error is not None:
            raise self.write_error
        return existed

    async def model_stats(self) -> dict[str, dict[str, int]]:
        return {model_id: dict(counts) for model_id, counts in self.stats.items()}


def _make_adapter(model_id: str):
    adapter = MagicMock()
    adapter.config.id = model_id
    adapter.config.name = model_id
    adapter.config.provider = "test"
    return adapter


def _by_id(body: dict) -> dict[str, dict]:
    return {model["model_id"]: model for model in body["models"]}


@pytest.fixture
def stored_settings(monkeypatch) -> dict[str, str]:
    """Database-backed settings as ``serving.batch.config`` reads them."""
    values: dict[str, str] = {}
    monkeypatch.setattr(
        batch_config_module, "config_value", lambda key, default=None: values.get(key, default)
    )
    return values


@pytest.fixture
async def admin_client(monkeypatch, stored_settings):
    store = _FakeBatchStore()
    router = FixedRouter()
    router.register_route(
        "public-model",
        [(_make_adapter("public-model"), 1.0)],
        aliases=["public-model-alias"],
    )
    router.register_route("provider/model", [(_make_adapter("provider/model"), 1.0)])
    router.register_route("staged-model", [(_make_adapter("staged-model"), 1.0)], published=False)

    app = FastAPI()
    app.state.services = AppServices(
        router=router,
        embedding_adapters={"bge-m3": _make_adapter("bge-m3")},
        operational_store=MagicMock(),
        batch_store=store,
        batch_model_limits=BatchModelLimits(store, ttl=3600.0),
    )
    app.include_router(admin_router.router)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    audit = AsyncMock()
    monkeypatch.setattr("serving.servers.routers.admin.batch.log_admin_action", audit)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.app = app  # type: ignore[attr-defined]
        client.audit = audit  # type: ignore[attr-defined]
        yield client, store


# --- listing -------------------------------------------------------------------


async def test_list_shows_every_canonical_chat_model_at_the_default(admin_client):
    client, _ = admin_client

    response = await client.get("/admin/batch/models", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert body["default_concurrency"] == 4
    # Aliases, unpublished routes and embedding models are not listed.
    assert [model["model_id"] for model in body["models"]] == ["provider/model", "public-model"]
    assert _by_id(body)["public-model"] == {
        "model_id": "public-model",
        "max_concurrency": None,
        "effective_concurrency": 4,
        "running": 0,
        "queued": 0,
    }


async def test_list_reports_overrides_load_and_models_only_the_store_knows(admin_client):
    client, store = admin_client
    store.limits = {"public-model": 0, "retired-model": 2}
    store.stats = {
        "public-model": {"running": 0, "queued": 5},
        "provider/model": {"running": 3, "queued": 7},
        "gone-model": {"running": 1, "queued": 0},
    }

    response = await client.get("/admin/batch/models", headers=ADMIN)

    assert response.status_code == 200
    models = _by_id(response.json())
    assert list(models) == ["gone-model", "provider/model", "public-model", "retired-model"]
    # A cap of 0 pauses the model while its requests wait.
    assert models["public-model"]["max_concurrency"] == 0
    assert models["public-model"]["effective_concurrency"] == 0
    assert models["public-model"]["queued"] == 5
    assert models["provider/model"]["max_concurrency"] is None
    assert models["provider/model"]["effective_concurrency"] == 4
    assert models["provider/model"]["running"] == 3
    # A cap left behind by a model that is no longer routed, and a model that
    # still has batch requests open, stay visible.
    assert models["retired-model"]["effective_concurrency"] == 2
    assert models["gone-model"]["running"] == 1
    assert models["gone-model"]["max_concurrency"] is None


async def test_list_reads_the_default_cap_live(admin_client, stored_settings):
    client, _ = admin_client
    stored_settings["BATCH_MODEL_CONCURRENCY"] = "9"

    response = await client.get("/admin/batch/models", headers=ADMIN)

    body = response.json()
    assert body["default_concurrency"] == 9
    assert _by_id(body)["public-model"]["effective_concurrency"] == 9


# --- setting and clearing ------------------------------------------------------------


async def test_patch_sets_a_cap(admin_client):
    client, store = admin_client
    store.stats = {"public-model": {"running": 1, "queued": 2}}

    response = await client.patch(
        "/admin/batch/models/public-model", json={"max_concurrency": 2}, headers=ADMIN
    )

    assert response.status_code == 200
    assert response.json() == {
        "model_id": "public-model",
        "max_concurrency": 2,
        "effective_concurrency": 2,
        "running": 1,
        "queued": 2,
    }
    assert store.writes == [("set", "public-model", 2, "admin-token")]
    assert store.limits == {"public-model": 2}


@pytest.mark.parametrize("cap", [0, 10_000])
async def test_patch_accepts_every_cap_in_range(admin_client, cap):
    client, store = admin_client

    response = await client.patch(
        "/admin/batch/models/public-model", json={"max_concurrency": cap}, headers=ADMIN
    )

    assert response.status_code == 200
    assert response.json()["effective_concurrency"] == cap
    assert store.limits == {"public-model": cap}


@pytest.mark.parametrize(
    "path",
    [
        "/admin/batch/models/provider/model",
        # The console encodes the id as one path segment.
        "/admin/batch/models/provider%2Fmodel",
    ],
)
async def test_patch_supports_slash_model_ids(admin_client, path):
    client, store = admin_client

    response = await client.patch(path, json={"max_concurrency": 1}, headers=ADMIN)

    assert response.status_code == 200
    assert response.json()["model_id"] == "provider/model"
    assert store.limits == {"provider/model": 1}


async def test_patch_null_clears_the_cap(admin_client, stored_settings):
    client, store = admin_client
    store.limits = {"public-model": 3}
    stored_settings["BATCH_MODEL_CONCURRENCY"] = "6"

    response = await client.patch(
        "/admin/batch/models/public-model", json={"max_concurrency": None}, headers=ADMIN
    )

    assert response.status_code == 200
    assert response.json()["max_concurrency"] is None
    assert response.json()["effective_concurrency"] == 6
    assert store.writes == [("delete", "public-model")]
    assert store.limits == {}


async def test_patch_clears_the_cap_of_a_model_no_longer_routed(admin_client):
    client, store = admin_client
    store.limits = {"retired-model": 2}

    response = await client.patch(
        "/admin/batch/models/retired-model", json={"max_concurrency": None}, headers=ADMIN
    )

    assert response.status_code == 200
    assert store.limits == {}


@pytest.mark.parametrize(
    "model_id",
    ["does-not-exist", "public-model-alias", "staged-model", "bge-m3", "retired-model"],
)
async def test_patch_setting_a_cap_needs_a_routed_canonical_chat_model(admin_client, model_id):
    client, store = admin_client
    store.limits = {"retired-model": 2}

    response = await client.patch(
        f"/admin/batch/models/{model_id}", json={"max_concurrency": 1}, headers=ADMIN
    )

    assert response.status_code == 404
    assert store.writes == []
    client.audit.assert_not_awaited()


async def test_patch_clearing_an_unknown_model_without_a_cap_is_404(admin_client):
    client, store = admin_client

    response = await client.patch(
        "/admin/batch/models/does-not-exist", json={"max_concurrency": None}, headers=ADMIN
    )

    assert response.status_code == 404
    assert store.writes == []


@pytest.mark.parametrize(
    "body",
    [
        {"max_concurrency": -1},
        {"max_concurrency": 10_001},
        {"max_concurrency": True},
        {"max_concurrency": "4"},
        {"max_concurrency": 2.5},
        {},
    ],
)
async def test_patch_refuses_a_value_that_is_not_a_cap(admin_client, body):
    client, store = admin_client

    response = await client.patch("/admin/batch/models/public-model", json=body, headers=ADMIN)

    assert response.status_code == 422
    assert store.writes == []


# --- what a write leaves behind --------------------------------------------------------


async def test_patch_applies_to_the_cached_caps_at_once(admin_client):
    client, _ = admin_client
    limits = client.app.state.services.batch_model_limits
    assert await limits.limit_for("public-model", 4) == 4

    response = await client.patch(
        "/admin/batch/models/public-model", json={"max_concurrency": 0}, headers=ADMIN
    )

    assert response.status_code == 200
    assert await limits.limit_for("public-model", 4) == 0


async def test_an_ambiguous_write_still_fences_the_cached_caps(admin_client):
    client, store = admin_client
    limits = client.app.state.services.batch_model_limits
    assert await limits.limit_for("public-model", 4) == 4
    store.write_error = RuntimeError("connection dropped after commit")

    with pytest.raises(RuntimeError, match="connection dropped after commit"):
        await client.patch(
            "/admin/batch/models/public-model", json={"max_concurrency": 0}, headers=ADMIN
        )

    assert await limits.limit_for("public-model", 4) == 0


async def test_patch_writes_an_audit_record(admin_client):
    client, store = admin_client
    store.limits = {"public-model": 3}

    await client.patch(
        "/admin/batch/models/public-model", json={"max_concurrency": 1}, headers=ADMIN
    )

    client.audit.assert_awaited_once()
    args = client.audit.await_args.args
    assert args[0] is client.app.state.services.operational_store
    kwargs = client.audit.await_args.kwargs
    assert kwargs["action"] == "batch.models.update"
    assert kwargs["actor"].type == "admin_token"
    assert kwargs["actor"].id is None
    assert kwargs["target"].type == "model"
    assert kwargs["target"].id == "public-model"
    assert kwargs["details"] == {
        "model_id": "public-model",
        "old_max_concurrency": 3,
        "new_max_concurrency": 1,
    }


# --- access ----------------------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not-the-admin-token"}])
async def test_both_endpoints_need_an_administrator(admin_client, headers):
    client, store = admin_client

    listed = await client.get("/admin/batch/models", headers=headers)
    patched = await client.patch(
        "/admin/batch/models/public-model", json={"max_concurrency": 0}, headers=headers
    )

    assert listed.status_code == 401
    assert patched.status_code == 401
    assert store.writes == []


async def test_without_a_database_both_endpoints_say_so(admin_client):
    client, _ = admin_client
    client.app.state.services.batch_store = None

    listed = await client.get("/admin/batch/models", headers=ADMIN)
    patched = await client.patch(
        "/admin/batch/models/public-model", json={"max_concurrency": 0}, headers=ADMIN
    )

    assert listed.status_code == 500
    assert listed.json()["detail"] == "Database not configured"
    assert patched.status_code == 500
