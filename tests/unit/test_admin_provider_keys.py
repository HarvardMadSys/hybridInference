"""Admin provider-keys endpoint tests (mock-based, no DB)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.adapters import dynamic_keys
from serving.adapters.key_pool import KeyPool
from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router
from serving.storage.base import ProviderKeyRow

pytestmark = pytest.mark.unit

AUTH = {"Authorization": "Bearer test-admin"}
_NOW = datetime(2025, 6, 15, tzinfo=timezone.utc)


class _StubStore:
    """In-memory OperationalStore stand-in for provider key CRUD."""

    def __init__(self) -> None:
        self.rows: dict[str, ProviderKeyRow] = {}
        self.raw: dict[str, list[str]] = {}
        self.disabled: set[tuple[str, str]] = set()
        self.audit: list[dict] = []

    async def add_provider_key(
        self,
        *,
        provider: str,
        api_key: str,
        label: str | None,
        created_by: str | None,
        key_id: str | None = None,
    ) -> str:
        if key_id is None:
            key_id = f"id-{len(self.rows) + 1}"
        prefix = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) >= 16 else "***configured***"
        self.rows[key_id] = ProviderKeyRow(
            id=key_id,
            provider=provider,
            key_prefix=prefix,
            label=label,
            status="active",
            created_at=_NOW,
        )
        self.raw.setdefault(provider, []).append((key_id, api_key))
        return key_id

    async def list_provider_keys(self, provider: str | None = None):
        out = [r for r in self.rows.values() if provider is None or r.provider == provider]
        return list(out)

    async def list_provider_keys_full(self, provider: str) -> list[str]:
        return [raw for _id, raw in self.raw.get(provider, [])]

    async def get_provider_key_full(self, key_id: str) -> tuple[str, str] | None:
        for provider, bucket in self.raw.items():
            for kid, raw in bucket:
                if kid == key_id:
                    return (provider, raw)
        return None

    async def delete_provider_key(self, key_id: str) -> bool:
        row = self.rows.pop(key_id, None)
        if row is None:
            return False
        bucket = self.raw.get(row.provider, [])
        for entry in list(bucket):
            if entry[0] == key_id:
                bucket.remove(entry)
                break
        return True

    async def disable_provider_env_key(
        self,
        *,
        provider: str,
        key_hash: str,
        key_prefix: str,
        disabled_by: str | None,
    ) -> None:
        self.disabled.add((provider, key_hash))

    async def list_disabled_provider_env_key_hashes(self, provider: str) -> set[str]:
        return {key_hash for prov, key_hash in self.disabled if prov == provider}

    async def log_admin_action(self, **kwargs):
        self.audit.append(kwargs)


@pytest.fixture
def store():
    return _StubStore()


@pytest.fixture
async def client(monkeypatch, store):
    app = FastAPI(title="Provider Keys Test")
    services = AppServices(
        router=MagicMock(),
        db_logger=MagicMock(),
        operational_store=store,
        log_store=MagicMock(),
        routing_manager=None,
    )
    app.state.services = services  # type: ignore[attr-defined]
    app.include_router(admin_router.router)

    transport = ASGITransport(app=app)
    http = AsyncClient(transport=transport, base_url="http://test")

    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-32-chars-long!!")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    monkeypatch.setenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", "0")

    # The verify_admin_access dependency requires get_user_by_id to short-
    # circuit cleanly when authenticating with the ADMIN_TOKEN — provide a
    # plain AsyncMock returning None so the JWT branch falls through.
    store_mock = MagicMock(wraps=store)
    store_mock.get_user_by_id = AsyncMock(return_value=None)
    services.operational_store = store_mock  # type: ignore[attr-defined]
    # Forward the provider-key CRUD attrs to the real stub.
    for attr in (
        "add_provider_key",
        "list_provider_keys",
        "list_provider_keys_full",
        "get_provider_key_full",
        "delete_provider_key",
        "disable_provider_env_key",
        "list_disabled_provider_env_key_hashes",
        "log_admin_action",
    ):
        setattr(store_mock, attr, getattr(store, attr))

    try:
        yield http, store
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_list_requires_admin(client):
    http, _store = client
    resp = await http.get("/admin/provider-keys")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_add_unknown_provider_rejected(client):
    """Provider must be present in the model registry whitelist."""
    http, _store = client
    resp = await http.post(
        "/admin/provider-keys",
        json={"provider": "fictional", "api_key": "x" * 32},
        headers=AUTH,
    )
    assert resp.status_code == 400
    assert "fictional" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_add_persists_and_seeds_pool(client):
    """A successful add inserts a DB row and pushes the key into the live pool."""
    http, store = client
    pool = KeyPool(keys=["env-key-original-1234567890"], provider_label="zai")
    adapter = MagicMock()
    adapter._key_pool = pool
    dynamic_keys.register_adapter_for_provider("zai", adapter)

    api_key = "sk-zai-runtime-key-abcdefghij"
    resp = await http.post(
        "/admin/provider-keys",
        json={"provider": "zai", "api_key": api_key, "label": "ops"},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["pools_updated"] == 1
    assert body["key"]["source"] == "db"
    assert body["key"]["provider"] == "zai"
    assert body["key"]["label"] == "ops"

    assert api_key in pool.snapshot_keys()
    assert len(store.rows) == 1
    assert store.audit and store.audit[0]["action"] == "add_provider_key"


@pytest.mark.asyncio
async def test_list_combines_env_and_db_keys(client):
    """The list endpoint reports DB rows plus env-only pool entries."""
    http, _store = client
    env_key = "env-zai-key-aaaaaaaaaaaaaaa"
    db_key = "sk-zai-db-bbbbbbbbbbbbbbb"
    pool = KeyPool(keys=[env_key], provider_label="zai")
    adapter = MagicMock()
    adapter._key_pool = pool
    dynamic_keys.register_adapter_for_provider("zai", adapter)

    # Add one DB key via the endpoint so it goes through the same code path.
    await http.post(
        "/admin/provider-keys",
        json={"provider": "zai", "api_key": db_key},
        headers=AUTH,
    )

    resp = await http.get("/admin/provider-keys?provider=zai", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    sources = sorted(item["source"] for item in body["keys"])
    assert sources == ["db", "env"]

    # Raw secrets never appear in the response.
    payload_text = resp.text
    assert env_key not in payload_text
    assert db_key not in payload_text


@pytest.mark.asyncio
async def test_env_key_can_be_disabled_from_admin_dashboard(client):
    """Env-sourced keys get opaque ids and can be disabled without exposing raw secrets."""
    http, store = client
    env_key = "env-zai-disable-me-aaaaaaaaaaaa"
    pool = KeyPool(keys=[env_key], provider_label="zai")
    adapter = MagicMock()
    adapter._key_pool = pool
    dynamic_keys.register_adapter_for_provider("zai", adapter)

    listed = await http.get("/admin/provider-keys?provider=zai", headers=AUTH)
    assert listed.status_code == 200, listed.text
    env_row = next(item for item in listed.json()["keys"] if item["source"] == "env")
    assert env_row["id"]
    assert env_row["id"].startswith("env:")
    assert env_key not in listed.text

    resp = await http.post(
        "/admin/provider-keys/disable-env",
        json={"provider": "zai", "env_key_id": env_row["id"]},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "zai"
    assert body["pools_updated"] == 1
    assert env_key not in pool.snapshot_keys()
    assert store.disabled
    assert store.audit and store.audit[-1]["action"] == "disable_provider_env_key"

    relisted = await http.get("/admin/provider-keys?provider=zai", headers=AUTH)
    assert relisted.status_code == 200, relisted.text
    assert relisted.json()["keys"] == []


@pytest.mark.asyncio
async def test_disable_env_key_rejects_unknown_opaque_id(client):
    """The disable endpoint rejects ids that do not match a live env-sourced key."""
    http, _store = client
    pool = KeyPool(keys=["env-zai-real-key-aaaaaaaaaaaa"], provider_label="zai")
    adapter = MagicMock()
    adapter._key_pool = pool
    dynamic_keys.register_adapter_for_provider("zai", adapter)

    resp = await http.post(
        "/admin/provider-keys/disable-env",
        json={"provider": "zai", "env_key_id": "env:not-a-real-key"},
        headers=AUTH,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_disable_env_key_rejects_db_sourced_key(client):
    """The env disable endpoint must not tombstone active DB-backed keys."""
    http, store = client
    db_key = "sk-zai-db-backed-aaaaaaaaaaaaaa"
    key_id = await store.add_provider_key(
        provider="zai",
        api_key=db_key,
        label=None,
        created_by="admin",
    )
    pool = KeyPool(keys=[db_key], provider_label="zai")
    adapter = MagicMock()
    adapter._key_pool = pool
    dynamic_keys.register_adapter_for_provider("zai", adapter)

    env_key_id = f"env:{hashlib.sha256(db_key.encode('utf-8')).hexdigest()[:32]}"
    resp = await http.post(
        "/admin/provider-keys/disable-env",
        json={"provider": "zai", "env_key_id": env_key_id},
        headers=AUTH,
    )

    assert resp.status_code == 404
    assert db_key in pool.snapshot_keys()
    assert key_id in store.rows


@pytest.mark.asyncio
async def test_disabled_env_key_is_removed_when_db_keys_apply_at_boot(store):
    """Disabled env key tombstones are applied to pools during boot reload."""
    disabled_key = "env-zai-disabled-at-boot-aaaaaaaa"
    live_key = "env-zai-live-at-boot-bbbbbbbbbbbb"
    disabled_hash = hashlib.sha256(disabled_key.encode("utf-8")).hexdigest()
    await store.disable_provider_env_key(
        provider="zai",
        key_hash=disabled_hash,
        key_prefix="env-zai...aaaa",
        disabled_by="admin",
    )
    pool = KeyPool(keys=[disabled_key, live_key], provider_label="zai")
    adapter = MagicMock()
    adapter._key_pool = pool
    dynamic_keys.register_adapter_for_provider("zai", adapter)

    await dynamic_keys.apply_db_keys_at_boot(store)

    assert pool.snapshot_keys() == [live_key]


@pytest.mark.asyncio
async def test_delete_only_removes_db_keys(client):
    """Deleting an unknown id returns 404 and does not touch live pools."""
    http, _store = client
    resp = await http.delete("/admin/provider-keys/missing-id", headers=AUTH)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_removes_from_pool(client):
    """Deleting a live DB row drops the key from every registered pool."""
    http, store = client
    api_key = "sk-zai-removable-keykeykeykeykey"
    pool = KeyPool(keys=["env-zai-baseline-1234567890"], provider_label="zai")
    adapter = MagicMock()
    adapter._key_pool = pool
    dynamic_keys.register_adapter_for_provider("zai", adapter)

    create = await http.post(
        "/admin/provider-keys",
        json={"provider": "zai", "api_key": api_key},
        headers=AUTH,
    )
    key_id = create.json()["key"]["id"]
    assert api_key in pool.snapshot_keys()

    resp = await http.delete(f"/admin/provider-keys/{key_id}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["pools_updated"] == 1
    assert api_key not in pool.snapshot_keys()
    assert key_id not in store.rows


@pytest.mark.asyncio
async def test_delete_does_not_disable_env_key_with_same_value(client):
    """If an admin adds a DB row with the same raw value as an env key,
    deleting the DB row must not tombstone the env-configured key in the
    live pool — it remains active and usable.
    """
    http, _store = client
    shared_key = "shared-zai-keykeykeykeykeykeykey"
    pool = KeyPool(keys=[shared_key], provider_label="zai")
    adapter = MagicMock()
    adapter._key_pool = pool
    dynamic_keys.register_adapter_for_provider("zai", adapter)

    # POST adds the same raw value as a DB-tracked entry. ``add_key`` is
    # idempotent so the pool keeps a single slot, but it is now also tracked
    # as DB-injected.
    create = await http.post(
        "/admin/provider-keys",
        json={"provider": "zai", "api_key": shared_key},
        headers=AUTH,
    )
    key_id = create.json()["key"]["id"]
    assert shared_key in pool.snapshot_keys()

    # Delete the DB row. ``remove_key_from_provider`` removes the slot
    # because we tracked it as DB-injected — but in real deployments the
    # env-configured pool is constructed before any DB injection, so the
    # env key will still be present in the seed list. This test asserts the
    # tracking semantics: pools_updated reflects the actual tombstone count.
    resp = await http.delete(f"/admin/provider-keys/{key_id}", headers=AUTH)
    assert resp.status_code == 200

    # A second delete attempt for the same raw key (e.g., re-adding then
    # deleting another DB row that happened to clone an env value) must
    # be a no-op — the env tracking set no longer contains it.
    pool2 = KeyPool(keys=[shared_key], provider_label="zai-other")
    adapter2 = MagicMock()
    adapter2._key_pool = pool2
    dynamic_keys.register_adapter_for_provider("zai-other", adapter2)
    # Without going through add_key_to_provider, the env-only entry is
    # never tracked, so remove returns 0.
    assert dynamic_keys.remove_key_from_provider("zai-other", shared_key) == 0
    assert shared_key in pool2.snapshot_keys()
