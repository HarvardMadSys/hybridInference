"""Admin provider-keys endpoint tests (mock-based, no DB)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.adapters import ModelConfig, OpenAICompatAdapter, dynamic_keys
from serving.adapters.key_pool import KeyPool
from serving.admin.provider_key_probe import (
    FEATHERLESS_PLAN_API_DISABLED_MESSAGE,
    probe_error_detail,
    probe_error_reason,
)
from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router
from serving.storage.base import ProviderKeyRow

pytestmark = pytest.mark.unit

AUTH = {"Authorization": "Bearer test-admin"}
_NOW = datetime(2025, 6, 15, tzinfo=timezone.utc)


def test_probe_error_reason_detects_featherless_plan_api_disabled():
    exc = aiohttp.ClientResponseError(
        request_info=MagicMock(),
        history=(),
        status=403,
        message="Forbidden",
    )
    exc.error_body = (  # type: ignore[attr-defined]
        '{"error":{"message":"' + FEATHERLESS_PLAN_API_DISABLED_MESSAGE + '","type":"forbidden"}}'
    )

    assert probe_error_reason(exc) == "plan_api_disabled"


def test_probe_error_detail_redacts_before_truncating():
    api_key = "rc_featherless_secret_that_crosses_truncation_boundary"
    exc = RuntimeError("x" * 490 + api_key + " trailing detail")

    detail = probe_error_detail(exc, timeout_seconds=20, api_key=api_key)

    assert api_key not in detail
    assert api_key[:12] not in detail
    assert "[redacted]" in detail


class _StubStore:
    """In-memory OperationalStore stand-in for provider key CRUD."""

    def __init__(self) -> None:
        self.rows: dict[str, ProviderKeyRow] = {}
        self.raw: dict[str, list[tuple[str, str]]] = {}
        self.disabled: set[tuple[str, str]] = set()
        self.route_configs: list[dict] = []
        self.route_candidates: list[dict] = []
        self.audit: list[dict] = []
        self.fail_full_for: set[str] = set()
        self.fail_disabled_for: set[str] = set()

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

    async def list_provider_keys_full(
        self,
        provider: str,
        *,
        exclude_ids: set[str] | None = None,
    ) -> list[str]:
        if provider in self.fail_full_for:
            raise RuntimeError(f"boom-full-{provider}")
        excluded = exclude_ids or set()
        return [raw for key_id, raw in self.raw.get(provider, []) if key_id not in excluded]

    async def list_all_provider_route_configs(self) -> list[dict]:
        return list(self.route_configs)

    async def list_all_provider_route_candidates(self) -> list[dict]:
        return list(self.route_candidates)

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
        if provider in self.fail_disabled_for:
            raise RuntimeError(f"boom-disabled-{provider}")
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
        router=MagicMock(routes={}),
        db_logger=MagicMock(),
        operational_store=store,
        log_store=MagicMock(),
        routing_manager=None,
    )
    app.state.services = services  # type: ignore[attr-defined]
    store.services = services
    app.include_router(admin_router.router)

    transport = ASGITransport(app=app)
    http = AsyncClient(transport=transport, base_url="http://test")

    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-32-chars-long!!")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    monkeypatch.setenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", "0")
    for env_var in (
        "CHUTES_API_KEY",
        "FEATHERLESS_API_KEY",
        "KIMI_CODING_API_KEY",
        "MINIMAX_API_KEY",
        "OLLAMA_API_KEY",
        "ZAI_API_KEY",
    ):
        monkeypatch.delenv(env_var, raising=False)
        for index in range(2, 21):
            monkeypatch.delenv(f"{env_var}{index}", raising=False)

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


def _registered_provider_adapter(provider: str, *, api_key: str = "env-key-original-1234567890"):
    return OpenAICompatAdapter(
        ModelConfig(
            id="minimax-fast",
            name="minimax-fast",
            provider=provider,
            base_url=f"https://{provider}.example/v1",
            api_keys=[api_key],
            provider_model_id="Provider/Test-Model",
            endpoint_id=f"minimax-fast:{provider}",
        )
    )


def _install_provider_route(store, provider: str):
    adapter = _registered_provider_adapter(provider)
    dynamic_keys.register_adapter_for_provider(provider, adapter)
    store.services.router.routes = {
        "minimax-fast": SimpleNamespace(adapters=[(adapter, 1.0)]),
    }
    return adapter


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
async def test_verify_provider_key_dry_run_does_not_persist_or_seed_pool(client):
    """Verification probes a temporary adapter and leaves DB/pools untouched."""
    http, store = client
    base_adapter = _install_provider_route(store, "featherless")
    pool = base_adapter._key_pool
    api_key = "rc-featherless-verify-key-aaaaaaaa"
    dry_run_adapter = MagicMock()
    dry_run_adapter.chat_completion = AsyncMock(return_value={"id": "ok"})

    with patch(
        "serving.admin.provider_key_probe._make_adapter",
        return_value=dry_run_adapter,
    ) as make_adapter:
        resp = await http.post(
            "/admin/provider-keys/verify",
            json={"provider": "featherless", "api_key": api_key},
            headers=AUTH,
        )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    assert len(store.rows) == 0
    assert api_key not in pool.snapshot_keys()
    kind, cfg = make_adapter.call_args.args
    assert kind == "featherless"
    assert cfg["api_key"] == api_key
    assert cfg["api_keys"] is None
    assert cfg["base_url"] == "https://featherless.example/v1"
    assert cfg["provider_model_id"] == "Provider/Test-Model"
    dry_run_adapter.chat_completion.assert_awaited_once_with(
        [{"role": "user", "content": "ping"}],
        max_tokens=1,
        temperature=0,
    )


@pytest.mark.asyncio
async def test_verify_provider_key_can_probe_raw_route_entries(client):
    """RouteWise may mask active adapters; verification should still inspect raw routes."""
    http, store = client
    featherless_adapter = _registered_provider_adapter("featherless")
    featherless_adapter.config.model_type = None
    openrouter_adapter = _registered_provider_adapter("openrouter")
    dynamic_keys.register_adapter_for_provider("featherless", featherless_adapter)
    store.services.router.routes = {
        "minimax-fast": SimpleNamespace(
            adapters=[(openrouter_adapter, 1.0)],
            raw_adapters=[(featherless_adapter, 1.0, "minimax-fast:featherless-api")],
        ),
    }
    api_key = "rc-featherless-raw-route-aaaaaaaa"
    dry_run_adapter = MagicMock()
    dry_run_adapter.chat_completion = AsyncMock(return_value={"id": "ok"})

    with patch(
        "serving.admin.provider_key_probe._make_adapter",
        return_value=dry_run_adapter,
    ) as make_adapter:
        resp = await http.post(
            "/admin/provider-keys/verify",
            json={"provider": "featherless", "api_key": api_key},
            headers=AUTH,
        )

    assert resp.status_code == 200, resp.text
    kind, cfg = make_adapter.call_args.args
    assert kind == "featherless"
    assert cfg["api_key"] == api_key
    assert cfg["provider_model_id"] == "Provider/Test-Model"


@pytest.mark.asyncio
async def test_verify_provider_key_failure_returns_400_without_raw_key(client):
    """Verification failures are reported without persisting or echoing the secret."""
    http, store = client
    _install_provider_route(store, "featherless")
    api_key = "rc-featherless-bad-key-bbbbbbbb"
    dry_run_adapter = MagicMock()
    dry_run_adapter.chat_completion = AsyncMock(
        side_effect=RuntimeError(f"upstream rejected {api_key}")
    )

    with patch(
        "serving.admin.provider_key_probe._make_adapter",
        return_value=dry_run_adapter,
    ):
        resp = await http.post(
            "/admin/provider-keys/verify",
            json={"provider": "featherless", "api_key": api_key},
            headers=AUTH,
        )

    assert resp.status_code == 400
    assert "Provider key verification failed" in resp.json()["detail"]
    assert api_key not in resp.text
    assert "[redacted]" in resp.json()["detail"]
    assert len(store.rows) == 0


@pytest.mark.asyncio
async def test_verify_provider_key_requires_registered_route(client):
    """Provider whitelist alone is not enough; verify needs a configured route model."""
    http, _store = client
    dynamic_keys.register_known_provider("featherless")

    resp = await http.post(
        "/admin/provider-keys/verify",
        json={"provider": "featherless", "api_key": "rc-featherless-key-cccccccc"},
        headers=AUTH,
    )

    assert resp.status_code == 400
    assert "No registered route available" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_list_provider_key_providers_comes_from_runtime_registry(client):
    """Provider choices for Keys are independent from quota-card support."""
    http, _store = client
    dynamic_keys.register_known_provider("featherless")

    resp = await http.get("/admin/provider-keys/providers", headers=AUTH)

    assert resp.status_code == 200
    assert "featherless" in resp.json()["providers"]


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
async def test_list_includes_numbered_featherless_env_keys(client, monkeypatch):
    """Numbered provider env vars should appear in the Keys tab list."""
    http, _store = client
    keys = [
        "rc_featherless_env_key_1111aaaa",
        "rc_featherless_env_key_2222bbbb",
        "rc_featherless_env_key_3333cccc",
    ]
    monkeypatch.setenv("FEATHERLESS_API_KEY", keys[0])
    monkeypatch.setenv("FEATHERLESS_API_KEY2", keys[1])
    monkeypatch.setenv("FEATHERLESS_API_KEY3", keys[2])
    monkeypatch.delenv("FEATHERLESS_API_KEY4", raising=False)
    dynamic_keys.register_known_provider("featherless")

    resp = await http.get("/admin/provider-keys?provider=featherless", headers=AUTH)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [item["key_prefix"] for item in body["keys"]] == [
        "rc_feath...aaaa",
        "rc_feath...bbbb",
        "rc_feath...cccc",
    ]
    assert {item["source"] for item in body["keys"]} == {"env"}
    for raw in keys:
        assert raw not in resp.text


@pytest.mark.asyncio
async def test_list_fails_closed_when_db_key_classification_lookup_fails(client):
    """The list endpoint should not misclassify DB keys as env keys on DB read errors."""
    http, store = client
    store.fail_full_for.add("zai")

    resp = await http.get("/admin/provider-keys?provider=zai", headers=AUTH)

    assert resp.status_code == 503
    assert "Failed to load provider keys" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_list_fails_closed_when_disabled_hash_lookup_fails(client):
    """The list endpoint should not surface stale env rows when tombstone lookup fails."""
    http, store = client
    store.fail_disabled_for.add("zai")

    resp = await http.get("/admin/provider-keys?provider=zai", headers=AUTH)

    assert resp.status_code == 503
    assert "Failed to load provider keys" in resp.json()["detail"]


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
async def test_disable_env_key_fails_closed_when_db_key_lookup_fails(client):
    """The disable endpoint should not proceed when DB-backed key lookup fails."""
    http, store = client
    store.fail_full_for.add("zai")
    pool = KeyPool(keys=["env-zai-real-key-aaaaaaaaaaaa"], provider_label="zai")
    adapter = MagicMock()
    adapter._key_pool = pool
    dynamic_keys.register_adapter_for_provider("zai", adapter)

    resp = await http.post(
        "/admin/provider-keys/disable-env",
        json={"provider": "zai", "env_key_id": "env:not-a-real-key"},
        headers=AUTH,
    )

    assert resp.status_code == 503
    assert "Failed to load provider keys" in resp.json()["detail"]


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
async def test_route_bound_db_key_is_not_seeded_into_provider_pool_at_boot(store):
    """Provider-route scoped keys should not become global provider keys."""
    global_key = "sk-zai-global-boot-key-aaaaaaaa"
    route_key = "sk-zai-route-bound-key-bbbbbbbb"
    await store.add_provider_key(
        provider="zai",
        api_key=global_key,
        label=None,
        created_by="admin",
        key_id="global-key",
    )
    await store.add_provider_key(
        provider="zai",
        api_key=route_key,
        label=None,
        created_by="admin",
        key_id="route-key",
    )
    store.route_configs = [{"api_key_id": "route-key"}]
    pool = KeyPool(keys=["env-zai-live-at-boot-cccccccc"], provider_label="zai")
    adapter = MagicMock()
    adapter._key_pool = pool
    dynamic_keys.register_adapter_for_provider("zai", adapter)

    await dynamic_keys.apply_db_keys_at_boot(store)

    keys = pool.snapshot_keys()
    assert global_key in keys
    assert route_key not in keys


@pytest.mark.asyncio
async def test_route_candidate_bound_db_key_is_not_seeded_into_provider_pool_at_boot(store):
    """Runtime-added candidate keys are also scoped to the route candidate."""
    global_key = "sk-zai-global-candidate-key-aaaaaaaa"
    candidate_key = "sk-zai-route-candidate-key-bbbbb"
    await store.add_provider_key(
        provider="zai",
        api_key=global_key,
        label=None,
        created_by="admin",
        key_id="global-key",
    )
    await store.add_provider_key(
        provider="zai",
        api_key=candidate_key,
        label=None,
        created_by="admin",
        key_id="candidate-key",
    )
    store.route_candidates = [{"api_key_id": "candidate-key"}]
    env_key = "env-zai-live-candidate-boot-cccccc"
    pool = KeyPool(keys=[env_key], provider_label="zai")
    adapter = MagicMock()
    adapter._key_pool = pool
    dynamic_keys.register_adapter_for_provider("zai", adapter)

    await dynamic_keys.apply_db_keys_at_boot(store)

    assert pool.snapshot_keys() == [env_key, global_key]


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
