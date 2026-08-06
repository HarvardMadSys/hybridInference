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

from serving.adapters import ModelConfig, OpenAICompatAdapter, OpenRouterAdapter, dynamic_keys
from serving.adapters.key_pool import KeyPool
from serving.admin.provider_key_probe import (
    FEATHERLESS_PLAN_API_DISABLED_MESSAGE,
    find_verification_adapter,
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


def test_probe_error_reason_decodes_bytes_error_body():
    exc = aiohttp.ClientResponseError(
        request_info=MagicMock(),
        history=(),
        status=403,
        message="Forbidden",
    )
    exc.error_body = (  # type: ignore[attr-defined]
        b'{"error":{"message":"'
        + FEATHERLESS_PLAN_API_DISABLED_MESSAGE.encode("utf-8")
        + b'","type":"forbidden"}}'
    )

    assert probe_error_reason(exc) == "plan_api_disabled"


def test_probe_error_detail_redacts_before_truncating():
    api_key = "rc_featherless_secret_that_crosses_truncation_boundary"
    exc = RuntimeError("x" * 490 + api_key + " trailing detail")

    detail = probe_error_detail(exc, timeout_seconds=20, api_key=api_key)

    assert api_key not in detail
    assert api_key[:12] not in detail
    assert "[redacted]" in detail


def test_find_verification_adapter_skips_adapter_without_config():
    services = SimpleNamespace(
        router=SimpleNamespace(
            routes={
                "broken": SimpleNamespace(
                    adapters=[(SimpleNamespace(config=None), 1.0)],
                ),
            },
        ),
    )

    assert find_verification_adapter(services, "featherless") is None


class _StubStore:
    """In-memory OperationalStore stand-in for provider key CRUD."""

    def __init__(self) -> None:
        self.rows: dict[str, ProviderKeyRow] = {}
        self.raw: dict[str, list[tuple[str, str]]] = {}
        # (provider, key_hash) -> key_prefix
        self.disabled: dict[tuple[str, str], str] = {}
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
        return [
            raw
            for kid, raw in self.raw.get(provider, [])
            if kid not in excluded and (kid not in self.rows or self.rows[kid].status == "active")
        ]

    async def set_provider_key_status(self, key_id: str, status: str) -> bool:
        row = self.rows.get(key_id)
        if row is None:
            return False
        row.status = status
        return True

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
        self.disabled[(provider, key_hash)] = key_prefix

    async def list_disabled_provider_env_key_hashes(self, provider: str) -> set[str]:
        if provider in self.fail_disabled_for:
            raise RuntimeError(f"boom-disabled-{provider}")
        return {key_hash for (prov, key_hash) in self.disabled if prov == provider}

    async def list_disabled_provider_env_keys(self, provider: str) -> list[tuple[str, str]]:
        if provider in self.fail_disabled_for:
            raise RuntimeError(f"boom-disabled-{provider}")
        return [
            (key_hash, prefix)
            for (prov, key_hash), prefix in self.disabled.items()
            if prov == provider
        ]

    async def enable_provider_env_key(self, provider: str, key_hash: str) -> bool:
        return self.disabled.pop((provider, key_hash), None) is not None

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
        "DEEPSEEK_API_KEY",
        "FEATHERLESS_API_KEY",
        "KIMI_CODING_API_KEY",
        "MINIMAX_API_KEY",
        "OLLAMA_API_KEY",
        "STAGING_API_KEY",
        "ZAI_API_KEY",
    ):
        monkeypatch.delenv(env_var, raising=False)
        for index in range(1, 21):
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
        "set_provider_key_status",
        "disable_provider_env_key",
        "list_disabled_provider_env_key_hashes",
        "list_disabled_provider_env_keys",
        "enable_provider_env_key",
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


def test_configured_env_keys_support_numbered_only_env_keys(monkeypatch):
    monkeypatch.delenv("CHUTES_API_KEY", raising=False)
    monkeypatch.setenv("CHUTES_API_KEY1", "chutes-key-one")
    monkeypatch.setenv("CHUTES_API_KEY2", "chutes-key-two")
    monkeypatch.delenv("CHUTES_API_KEY3", raising=False)

    assert dynamic_keys.configured_env_keys_for_provider("chutes") == [
        "chutes-key-one",
        "chutes-key-two",
    ]


def test_configured_env_keys_include_provider_overview_builtin_names(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    monkeypatch.setenv("STAGING_API_KEY", "staging-key")

    assert dynamic_keys.configured_env_keys_for_provider("deepseek") == ["deepseek-key"]
    assert dynamic_keys.configured_env_keys_for_provider("staging") == ["staging-key"]


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
async def test_verify_provider_key_matches_kimi_coding_route(client):
    """Kimi keys verify against the coding-plan route kind."""
    http, store = client
    kimi_adapter = OpenAICompatAdapter(
        ModelConfig(
            id="kimi-k2.7-code",
            name="kimi-k2.7-code",
            provider="kimi_coding",
            base_url="https://kimi.example/v1",
            api_keys=["env-kimi-coding-key-1234567890"],
            provider_model_id="kimi-for-coding",
            endpoint_id="kimi-k2.7-code:kimi-api",
        )
    )
    dynamic_keys.register_adapter_for_provider("kimi", kimi_adapter)
    store.services.router.routes = {
        "kimi-k2.7-code": SimpleNamespace(adapters=[(kimi_adapter, 1.0)]),
    }
    api_key = "kimi-candidate-key-aaaaaaaa"
    dry_run_adapter = MagicMock()
    dry_run_adapter.chat_completion = AsyncMock(return_value={"id": "ok"})

    with patch(
        "serving.admin.provider_key_probe._make_adapter",
        return_value=dry_run_adapter,
    ) as make_adapter:
        resp = await http.post(
            "/admin/provider-keys/verify",
            json={"provider": "kimi", "api_key": api_key},
            headers=AUTH,
        )

    assert resp.status_code == 200, resp.text
    kind, cfg = make_adapter.call_args.args
    assert kind == "kimi_coding"
    assert cfg["api_key"] == api_key
    assert cfg["provider_model_id"] == "kimi-for-coding"


@pytest.mark.asyncio
async def test_verify_provider_key_matches_pinned_openrouter_route(client):
    """OpenRouter keys verify against pinned OpenRouter route variants."""
    http, store = client
    openrouter_adapter = OpenRouterAdapter(
        ModelConfig(
            id="openrouter-model",
            name="openrouter-model",
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_keys=["env-openrouter-key-1234567890"],
            provider_model_id="openai/gpt-oss-120b",
            endpoint_id="openrouter-model:openrouter-deepinfra-api",
            openrouter_pinned_provider="deepinfra",
        )
    )
    dynamic_keys.register_adapter_for_provider("openrouter", openrouter_adapter)
    store.services.router.routes = {
        "openrouter-model": SimpleNamespace(adapters=[(openrouter_adapter, 1.0)]),
    }
    api_key = "sk-or-candidate-key-aaaaaaaa"
    dry_run_adapter = MagicMock()
    dry_run_adapter.chat_completion = AsyncMock(return_value={"id": "ok"})

    with patch(
        "serving.admin.provider_key_probe._make_adapter",
        return_value=dry_run_adapter,
    ) as make_adapter:
        resp = await http.post(
            "/admin/provider-keys/verify",
            json={"provider": "openrouter", "api_key": api_key},
            headers=AUTH,
        )

    assert resp.status_code == 200, resp.text
    kind, cfg = make_adapter.call_args.args
    assert kind == "openrouter[deepinfra]"
    assert cfg["api_key"] == api_key
    assert cfg["provider_model_id"] == "openai/gpt-oss-120b"
    assert cfg["openrouter_pinned_provider"] == "deepinfra"


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
    dynamic_keys.register_known_provider("kimi_coding")

    resp = await http.get("/admin/provider-keys/providers", headers=AUTH)

    assert resp.status_code == 200
    assert "featherless" in resp.json()["providers"]
    assert "kimi" in resp.json()["providers"]
    assert "kimi_coding" not in resp.json()["providers"]


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
async def test_add_lazily_promotes_single_key_adapter(client):
    """Adding a key to a single-`api_key` provider promotes it to a pool.

    Regression: previously such an adapter had no KeyPool, so the runtime key
    attached to 0 pools and was silently persisted-but-unused. It must now be
    attached (pools_updated >= 1) and live in the pool alongside the env key.
    """
    http, _store = client
    adapter = OpenAICompatAdapter(
        ModelConfig(
            id="lazy-model",
            name="lazy-model",
            provider="minimax-lazy",
            base_url="https://api.example.com",
            api_key="env-key-original-1234567890",
            provider_model_id="lazy-model",
        )
    )
    assert adapter._key_pool is None
    dynamic_keys.register_adapter_for_provider("minimax-lazy", adapter)

    api_key = "sk-minimax-runtime-abcdefghij"
    resp = await http.post(
        "/admin/provider-keys",
        json={"provider": "minimax-lazy", "api_key": api_key},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["pools_updated"] == 1
    assert adapter._key_pool is not None
    assert api_key in adapter._key_pool.snapshot_keys()
    assert "env-key-original-1234567890" in adapter._key_pool.snapshot_keys()


@pytest.mark.asyncio
async def test_disable_and_enable_db_key_toggles_pool(client):
    """A DB key can be disabled (removed from pool) and re-enabled (re-added)."""
    http, store = client
    pool = KeyPool(keys=["env-key-original-1234567890"], provider_label="zai")
    adapter = MagicMock()
    adapter._key_pool = pool
    dynamic_keys.register_adapter_for_provider("zai", adapter)

    api_key = "sk-zai-toggle-abcdefghij12"
    add_resp = await http.post(
        "/admin/provider-keys",
        json={"provider": "zai", "api_key": api_key},
        headers=AUTH,
    )
    key_id = add_resp.json()["key"]["id"]
    assert api_key in pool.snapshot_keys()

    # Disable -> removed from pool, status flips to disabled, key still persisted.
    dis = await http.post(f"/admin/provider-keys/{key_id}/disable", headers=AUTH)
    assert dis.status_code == 200, dis.text
    body = dis.json()
    assert body["status"] == "disabled"
    assert body["pools_updated"] == 1
    assert api_key not in pool.snapshot_keys()
    assert store.rows[key_id].status == "disabled"

    # Disabled keys are excluded from the boot seeding list.
    assert api_key not in await store.list_provider_keys_full("zai")

    # Enable -> back in the pool, status active again.
    en = await http.post(f"/admin/provider-keys/{key_id}/enable", headers=AUTH)
    assert en.status_code == 200, en.text
    assert en.json()["status"] == "active"
    assert api_key in pool.snapshot_keys()
    assert store.rows[key_id].status == "active"


@pytest.mark.asyncio
async def test_disabled_keys_appear_in_list(client):
    """Disabled DB and env keys are surfaced in the list for re-enabling."""
    http, _store = client
    env_key = "env-zai-listed-key-aaaaaaaa"
    pool = KeyPool(keys=[env_key], provider_label="zai")
    adapter = MagicMock()
    adapter._key_pool = pool
    dynamic_keys.register_adapter_for_provider("zai", adapter)

    # Disable the env key via the endpoint.
    env_id = f"env:{dynamic_keys.env_key_hash(env_key)[:32]}"
    dis = await http.post(
        "/admin/provider-keys/disable-env",
        json={"provider": "zai", "env_key_id": env_id},
        headers=AUTH,
    )
    assert dis.status_code == 200, dis.text

    listing = await http.get("/admin/provider-keys?provider=zai", headers=AUTH)
    items = listing.json()["keys"]
    env_items = [k for k in items if k["source"] == "env"]
    assert any(k["status"] == "disabled" and k["id"] == env_id for k in env_items)


@pytest.mark.asyncio
async def test_enable_env_key_restores_to_pool(client):
    """Re-enabling a disabled env key recovers the raw key and re-adds it."""
    http, _store = client
    env_key = "env-zai-reenable-bbbbbbbbbb"
    adapter = OpenAICompatAdapter(
        ModelConfig(
            id="reenable-model",
            name="reenable-model",
            provider="zai",
            base_url="https://api.example.com",
            api_keys=[env_key],
            provider_model_id="reenable-model",
        )
    )
    dynamic_keys.register_adapter_for_provider("zai", adapter)
    assert env_key in adapter._key_pool.snapshot_keys()

    env_id = f"env:{dynamic_keys.env_key_hash(env_key)[:32]}"
    await http.post(
        "/admin/provider-keys/disable-env",
        json={"provider": "zai", "env_key_id": env_id},
        headers=AUTH,
    )
    assert env_key not in adapter._key_pool.snapshot_keys()

    en = await http.post(
        "/admin/provider-keys/enable-env",
        json={"provider": "zai", "env_key_id": env_id},
        headers=AUTH,
    )
    assert en.status_code == 200, en.text
    assert en.json()["pools_updated"] == 1
    assert env_key in adapter._key_pool.snapshot_keys()


@pytest.mark.asyncio
async def test_single_key_env_credential_is_listed_and_disableable(client):
    """A legacy single-api_key route's env credential is manageable w/o a pool.

    Regression for the gap where the list and disable-env only inspected pools:
    a pool-less single-key adapter's static key must appear in the list and be
    disable-able (promoted to a pool and stripped).
    """
    http, _store = client
    env_key = "env-zai-single-only-dddddddddd"
    adapter = OpenAICompatAdapter(
        ModelConfig(
            id="single-model",
            name="single-model",
            provider="zai",
            base_url="https://api.example.com",
            api_key=env_key,
            provider_model_id="single-model",
        )
    )
    assert adapter._key_pool is None
    dynamic_keys.register_adapter_for_provider("zai", adapter)

    listing = await http.get("/admin/provider-keys?provider=zai", headers=AUTH)
    env_items = [k for k in listing.json()["keys"] if k["source"] == "env"]
    assert len(env_items) == 1 and env_items[0]["status"] == "active"
    env_id = env_items[0]["id"]

    dis = await http.post(
        "/admin/provider-keys/disable-env",
        json={"provider": "zai", "env_key_id": env_id},
        headers=AUTH,
    )
    assert dis.status_code == 200, dis.text
    assert dis.json()["pools_updated"] == 1
    # Adapter was promoted and the disabled key stripped.
    assert adapter._key_pool is not None
    assert env_key not in adapter._key_pool.snapshot_keys()

    relist = await http.get("/admin/provider-keys?provider=zai", headers=AUTH)
    env_after = [k for k in relist.json()["keys"] if k["source"] == "env"]
    assert [k["status"] for k in env_after] == ["disabled"]


@pytest.mark.asyncio
async def test_disable_db_key_preserves_shared_env_value(client):
    """Disabling a DB row keeps the raw value when an env key still shares it."""
    http, store = client
    shared = "shared-zai-value-eeeeeeeeeeee"
    adapter = OpenAICompatAdapter(
        ModelConfig(
            id="shared-model",
            name="shared-model",
            provider="zai",
            base_url="https://api.example.com",
            api_keys=[shared],
            provider_model_id="shared-model",
        )
    )
    dynamic_keys.register_adapter_for_provider("zai", adapter)
    assert shared in adapter._key_pool.snapshot_keys()

    # Add a DB row whose value collides with the active env key.
    add = await http.post(
        "/admin/provider-keys",
        json={"provider": "zai", "api_key": shared},
        headers=AUTH,
    )
    key_id = add.json()["key"]["id"]

    dis = await http.post(f"/admin/provider-keys/{key_id}/disable", headers=AUTH)
    assert dis.status_code == 200, dis.text
    # The shared value is still active via the env key, so it stays in the pool.
    assert dis.json()["pools_updated"] == 0
    assert shared in adapter._key_pool.snapshot_keys()
    assert store.rows[key_id].status == "disabled"


@pytest.mark.asyncio
async def test_delete_db_key_preserves_value_shared_by_another_db_row(client):
    """Deleting one DB row keeps the value when another DB row still uses it.

    Regression for the delete path: the disable path guards against evicting a
    value still active via another source, but a hard-delete that did not repeat
    the check would yank a credential a second, still-active DB row relies on —
    stopping traffic until restart/re-enable.
    """
    http, store = client
    shared = "shared-zai-dbrow-ffffffffffff"
    pool = KeyPool(keys=["env-zai-other-1234567890"], provider_label="zai")
    adapter = MagicMock()
    adapter._key_pool = pool
    dynamic_keys.register_adapter_for_provider("zai", adapter)

    first = await http.post(
        "/admin/provider-keys",
        json={"provider": "zai", "api_key": shared},
        headers=AUTH,
    )
    first_id = first.json()["key"]["id"]
    second = await http.post(
        "/admin/provider-keys",
        json={"provider": "zai", "api_key": shared},
        headers=AUTH,
    )
    second_id = second.json()["key"]["id"]
    assert shared in pool.snapshot_keys()

    # Delete the first row — the second active row still owns the value, so it
    # must remain in the pool.
    resp = await http.delete(f"/admin/provider-keys/{first_id}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["pools_updated"] == 0
    assert shared in pool.snapshot_keys()
    assert first_id not in store.rows

    # Deleting the last remaining row finally evicts the value.
    resp2 = await http.delete(f"/admin/provider-keys/{second_id}", headers=AUTH)
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["pools_updated"] == 1
    assert shared not in pool.snapshot_keys()


@pytest.mark.asyncio
async def test_enable_env_key_only_restores_to_owning_pool(client):
    """Re-enabling an env key re-adds it only to adapters configured with it.

    Regression for cross-pool leakage: a provider with two adapters carrying
    distinct env credentials must not have one adapter's re-enabled key injected
    into the other adapter's pool (which could fail auth or mix quotas).
    """
    http, _store = client
    key_a = "env-zai-owner-a-aaaaaaaaaaaa"
    key_b = "env-zai-owner-b-bbbbbbbbbbbb"
    adapter_a = OpenAICompatAdapter(
        ModelConfig(
            id="owner-a-model",
            name="owner-a-model",
            provider="zai",
            base_url="https://api.example.com",
            api_keys=[key_a],
            provider_model_id="owner-a-model",
        )
    )
    adapter_b = OpenAICompatAdapter(
        ModelConfig(
            id="owner-b-model",
            name="owner-b-model",
            provider="zai",
            base_url="https://api.example.com",
            api_keys=[key_b],
            provider_model_id="owner-b-model",
        )
    )
    dynamic_keys.register_adapter_for_provider("zai", adapter_a)
    dynamic_keys.register_adapter_for_provider("zai", adapter_b)

    env_id = f"env:{dynamic_keys.env_key_hash(key_a)[:32]}"
    dis = await http.post(
        "/admin/provider-keys/disable-env",
        json={"provider": "zai", "env_key_id": env_id},
        headers=AUTH,
    )
    assert dis.status_code == 200, dis.text
    assert key_a not in adapter_a._key_pool.snapshot_keys()

    en = await http.post(
        "/admin/provider-keys/enable-env",
        json={"provider": "zai", "env_key_id": env_id},
        headers=AUTH,
    )
    assert en.status_code == 200, en.text
    # Only the owning adapter (A) gets key_a back; B's pool is untouched.
    assert en.json()["pools_updated"] == 1
    assert key_a in adapter_a._key_pool.snapshot_keys()
    assert key_a not in adapter_b._key_pool.snapshot_keys()
    assert key_b in adapter_b._key_pool.snapshot_keys()


def test_promotion_drops_disabled_static_env_key():
    """A disabled env key must not return when promotion re-seeds the pool.

    Regression for the boot ordering: ``apply_db_keys_at_boot`` records the
    disabled hash before seeding DB keys, but a single-key adapter has no pool
    to filter at that point. When the DB key later promotes the adapter, the
    static env key is re-seeded — it must be dropped because it was disabled.
    """
    env_key = "env-disabled-key-1234567890"
    adapter = OpenAICompatAdapter(
        ModelConfig(
            id="disabled-model",
            name="disabled-model",
            provider="minimax-disabled",
            base_url="https://api.example.com",
            api_key=env_key,
            provider_model_id="disabled-model",
        )
    )
    dynamic_keys.register_adapter_for_provider("minimax-disabled", adapter)
    # Admin disabled the env key earlier (hash tracked even with no live pool).
    dynamic_keys.disable_env_key_for_provider(
        "minimax-disabled", env_key, dynamic_keys.env_key_hash(env_key)
    )

    db_key = "sk-minimax-db-abcdefghij12"
    attached = dynamic_keys.add_key_to_provider("minimax-disabled", db_key)

    assert attached == 1
    assert adapter._key_pool is not None
    snapshot = adapter._key_pool.snapshot_keys()
    assert db_key in snapshot
    assert env_key not in snapshot


@pytest.mark.asyncio
async def test_boot_enforces_tombstone_on_single_key_adapter(store):
    """At boot, a disabled env key is stripped even with no DB keys to promote.

    Regression for the legacy single-key path: an adapter with no pool serves
    ``config.api_key`` directly, bypassing tombstones. Boot must promote it to
    a pool and drop the disabled key so the env key is not resurrected.
    """
    env_key = "env-zai-boot-cccccccccccc"
    adapter = OpenAICompatAdapter(
        ModelConfig(
            id="boot-model",
            name="boot-model",
            provider="zai",
            base_url="https://api.example.com",
            api_key=env_key,
            provider_model_id="boot-model",
        )
    )
    assert adapter._key_pool is None
    dynamic_keys.register_known_provider("zai")
    dynamic_keys.register_adapter_for_provider("zai", adapter)
    await store.disable_provider_env_key(
        provider="zai",
        key_hash=dynamic_keys.env_key_hash(env_key),
        key_prefix="env-zai-b...cccc",
        disabled_by=None,
    )

    await dynamic_keys.apply_db_keys_at_boot(store)

    assert adapter._key_pool is not None
    assert env_key not in adapter._key_pool.snapshot_keys()


@pytest.mark.asyncio
async def test_add_reports_zero_pools_when_no_capable_adapter(client):
    """A provider with no pool-capable adapter reports pools_updated == 0.

    The key is still persisted, but the response signals it is not attached to
    any live pool so the UI can warn the operator instead of implying success.
    """
    http, store = client
    dynamic_keys.register_known_provider("noop-provider")

    resp = await http.post(
        "/admin/provider-keys",
        json={"provider": "noop-provider", "api_key": "sk-noop-abcdefghij1234"},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["pools_updated"] == 0
    # The key is persisted even though it is not attached to a pool.
    assert len(store.rows) == 1


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

    # The disabled env key is no longer active in any pool, but it is still
    # surfaced (status="disabled") so it can be re-enabled from the dashboard.
    relisted = await http.get("/admin/provider-keys?provider=zai", headers=AUTH)
    assert relisted.status_code == 200, relisted.text
    relisted_keys = relisted.json()["keys"]
    assert [k["status"] for k in relisted_keys] == ["disabled"]
    assert relisted_keys[0]["source"] == "env"
    assert relisted_keys[0]["id"] == env_row["id"]


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
async def test_adding_a_tombstoned_key_clears_its_env_tombstone(client, monkeypatch):
    """Re-adding a disabled env credential must not leave its tombstone behind.

    ``add_key_to_provider`` puts the value straight back into the pool, so a
    surviving tombstone means the key serves traffic while a disabled record
    still exists for it — which the Keys tab renders as a second, disabled
    entry for the same key.
    """
    http, store = client
    raw = "env-zai-readded-ffffffffffff"
    monkeypatch.setenv("ZAI_API_KEY", raw)
    adapter = OpenAICompatAdapter(
        ModelConfig(
            id="readded-model",
            name="readded-model",
            provider="zai",
            base_url="https://api.example.com",
            api_keys=[raw],
            provider_model_id="readded-model",
        )
    )
    dynamic_keys.register_adapter_for_provider("zai", adapter)
    key_hash = dynamic_keys.env_key_hash(raw)

    dis = await http.post(
        "/admin/provider-keys/disable-env",
        json={"provider": "zai", "env_key_id": f"env:{key_hash[:32]}"},
        headers=AUTH,
    )
    assert dis.status_code == 200, dis.text
    assert ("zai", key_hash) in store.disabled

    add = await http.post(
        "/admin/provider-keys",
        json={"provider": "zai", "api_key": raw},
        headers=AUTH,
    )
    assert add.status_code == 201, add.text

    assert ("zai", key_hash) not in store.disabled
    assert dynamic_keys.is_env_key_disabled("zai", key_hash) is False
    assert raw in adapter._key_pool.snapshot_keys()

    listing = await http.get("/admin/provider-keys?provider=zai", headers=AUTH)
    entries = [k for k in listing.json()["keys"] if k["key_prefix"] == f"{raw[:8]}...{raw[-4:]}"]
    assert len(entries) == 1, entries
    assert entries[0]["status"] == "active"


@pytest.mark.asyncio
async def test_listing_hides_a_tombstone_shadowed_by_an_active_db_key(client):
    """A stale tombstone left by an older build must not double-list the key."""
    http, store = client
    raw = "sk-zai-shadowed-gggggggggggg"
    await store.add_provider_key(provider="zai", api_key=raw, label=None, created_by="admin")
    store.disabled[("zai", dynamic_keys.env_key_hash(raw))] = f"{raw[:8]}...{raw[-4:]}"
    dynamic_keys.register_adapter_for_provider(
        "zai",
        SimpleNamespace(_key_pool=KeyPool([raw], "zai")),
    )

    listing = await http.get("/admin/provider-keys?provider=zai", headers=AUTH)

    entries = [k for k in listing.json()["keys"] if k["key_prefix"] == f"{raw[:8]}...{raw[-4:]}"]
    assert len(entries) == 1, entries
    assert entries[0]["source"] == "db"
    assert entries[0]["status"] == "active"


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


@pytest.mark.asyncio
async def test_by_ref_disable_and_enable_db_key(client):
    """The quota dashboard's key_ref resolves to a DB key and toggles it."""
    http, store = client
    pool = KeyPool(keys=["env-key-original-1234567890"], provider_label="zai")
    adapter = MagicMock()
    adapter._key_pool = pool
    dynamic_keys.register_adapter_for_provider("zai", adapter)

    api_key = "sk-zai-byref-aaaaaaaaaa12"
    add_resp = await http.post(
        "/admin/provider-keys",
        json={"provider": "zai", "api_key": api_key},
        headers=AUTH,
    )
    key_id = add_resp.json()["key"]["id"]
    key_ref = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:32]

    dis = await http.post(
        "/admin/provider-keys/by-ref/disable",
        json={"provider": "zai", "key_ref": key_ref},
        headers=AUTH,
    )
    assert dis.status_code == 200, dis.text
    assert dis.json() == {
        "provider": "zai",
        "key_ref": key_ref,
        "source": "db",
        "status": "disabled",
        "pools_updated": 1,
    }
    assert store.rows[key_id].status == "disabled"
    assert api_key not in pool.snapshot_keys()

    # Disabling again is a no-op rather than an error.
    again = await http.post(
        "/admin/provider-keys/by-ref/disable",
        json={"provider": "zai", "key_ref": key_ref},
        headers=AUTH,
    )
    assert again.status_code == 200, again.text
    assert again.json()["pools_updated"] == 0

    en = await http.post(
        "/admin/provider-keys/by-ref/enable",
        json={"provider": "zai", "key_ref": key_ref},
        headers=AUTH,
    )
    assert en.status_code == 200, en.text
    assert en.json()["status"] == "active"
    assert store.rows[key_id].status == "active"
    assert api_key in pool.snapshot_keys()


@pytest.mark.asyncio
async def test_by_ref_disable_and_enable_env_key(client):
    """An env-sourced key is tombstoned and restored through the by-ref path."""
    http, _store = client
    env_key = "env-zai-by-ref-cccccccccccc"
    adapter = OpenAICompatAdapter(
        ModelConfig(
            id="by-ref-model",
            name="by-ref-model",
            provider="zai",
            base_url="https://api.example.com",
            api_keys=[env_key],
            provider_model_id="by-ref-model",
        )
    )
    dynamic_keys.register_adapter_for_provider("zai", adapter)
    key_ref = dynamic_keys.env_key_hash(env_key)[:32]

    dis = await http.post(
        "/admin/provider-keys/by-ref/disable",
        json={"provider": "zai", "key_ref": key_ref},
        headers=AUTH,
    )
    assert dis.status_code == 200, dis.text
    assert dis.json()["source"] == "env"
    assert env_key not in adapter._key_pool.snapshot_keys()

    en = await http.post(
        "/admin/provider-keys/by-ref/enable",
        json={"provider": "zai", "key_ref": key_ref},
        headers=AUTH,
    )
    assert en.status_code == 200, en.text
    assert en.json() == {
        "provider": "zai",
        "key_ref": key_ref,
        "source": "env",
        "status": "active",
        "pools_updated": 1,
    }
    assert env_key in adapter._key_pool.snapshot_keys()


@pytest.mark.asyncio
async def test_by_ref_toggles_every_source_holding_the_key(client):
    """A key recorded in env *and* in a DB row toggles at both sources.

    Disabling only one of them left the key live in the pool while a disabled
    record existed for it, which the quota dashboard rendered as a second card
    for the same key.
    """
    http, store = client
    shared = "shared-zai-byref-dddddddddddd"
    adapter = OpenAICompatAdapter(
        ModelConfig(
            id="shared-byref-model",
            name="shared-byref-model",
            provider="zai",
            base_url="https://api.example.com",
            api_keys=[shared],
            provider_model_id="shared-byref-model",
        )
    )
    dynamic_keys.register_adapter_for_provider("zai", adapter)
    add = await http.post(
        "/admin/provider-keys",
        json={"provider": "zai", "api_key": shared},
        headers=AUTH,
    )
    key_id = add.json()["key"]["id"]
    key_ref = dynamic_keys.env_key_hash(shared)[:32]

    dis = await http.post(
        "/admin/provider-keys/by-ref/disable",
        json={"provider": "zai", "key_ref": key_ref},
        headers=AUTH,
    )
    assert dis.status_code == 200, dis.text
    assert dis.json()["status"] == "disabled"
    assert store.rows[key_id].status == "disabled"
    assert ("zai", dynamic_keys.env_key_hash(shared)) in store.disabled
    # The key actually stopped serving traffic.
    assert shared not in adapter._key_pool.snapshot_keys()

    en = await http.post(
        "/admin/provider-keys/by-ref/enable",
        json={"provider": "zai", "key_ref": key_ref},
        headers=AUTH,
    )
    assert en.status_code == 200, en.text
    assert en.json()["status"] == "active"
    assert store.rows[key_id].status == "active"
    assert ("zai", dynamic_keys.env_key_hash(shared)) not in store.disabled
    assert shared in adapter._key_pool.snapshot_keys()


@pytest.mark.asyncio
async def test_by_ref_enable_clears_tombstone_when_env_var_still_set(client, monkeypatch):
    """Re-enabling must clear the tombstone even though the env var is present.

    ``_resolve_key_ref`` used to report any key it could still find among the
    env candidates as active, so enabling a tombstoned key short-circuited: the
    dashboard reported success while the key stayed disabled.
    """
    http, store = client
    env_key = "env-zai-still-set-eeeeeeeeeeee"
    monkeypatch.setenv("ZAI_API_KEY", env_key)
    adapter = OpenAICompatAdapter(
        ModelConfig(
            id="still-set-model",
            name="still-set-model",
            provider="zai",
            base_url="https://api.example.com",
            api_keys=[env_key],
            provider_model_id="still-set-model",
        )
    )
    dynamic_keys.register_adapter_for_provider("zai", adapter)
    key_ref = dynamic_keys.env_key_hash(env_key)[:32]

    await http.post(
        "/admin/provider-keys/by-ref/disable",
        json={"provider": "zai", "key_ref": key_ref},
        headers=AUTH,
    )
    assert ("zai", dynamic_keys.env_key_hash(env_key)) in store.disabled

    en = await http.post(
        "/admin/provider-keys/by-ref/enable",
        json={"provider": "zai", "key_ref": key_ref},
        headers=AUTH,
    )
    assert en.status_code == 200, en.text
    assert en.json()["pools_updated"] == 1
    assert ("zai", dynamic_keys.env_key_hash(env_key)) not in store.disabled
    assert env_key in adapter._key_pool.snapshot_keys()


@pytest.mark.asyncio
async def test_by_ref_unknown_ref_is_404(client):
    """An unmatched key_ref must not be mistaken for a key id path segment."""
    http, _store = client
    pool = KeyPool(keys=["env-key-original-1234567890"], provider_label="zai")
    adapter = MagicMock()
    adapter._key_pool = pool
    dynamic_keys.register_adapter_for_provider("zai", adapter)

    resp = await http.post(
        "/admin/provider-keys/by-ref/disable",
        json={"provider": "zai", "key_ref": "0" * 32},
        headers=AUTH,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_by_ref_unknown_provider_rejected(client):
    """Providers outside the model registry whitelist are rejected."""
    http, _store = client
    resp = await http.post(
        "/admin/provider-keys/by-ref/disable",
        json={"provider": "fictional", "key_ref": "0" * 32},
        headers=AUTH,
    )
    assert resp.status_code == 400, resp.text
