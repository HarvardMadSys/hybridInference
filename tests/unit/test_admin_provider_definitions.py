from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from serving.schemas_admin import UpdateProviderDefinitionRequest
from serving.servers.routers.admin import provider_definitions
from serving.servers.routers.admin.provider_definitions import _configured_provider_specs
from serving.storage.base import ProviderDefinitionRow


class FakeProviderDefinitionStore:
    def __init__(self, rows: dict[str, ProviderDefinitionRow] | None = None):
        self.rows = rows or {}
        self.upserts: list[dict[str, object]] = []
        self.deleted_keys: list[str] = []

    async def get_provider_definition(self, provider: str):
        return self.rows.get(provider)

    async def upsert_provider_definition(
        self,
        *,
        provider: str,
        display_name: str,
        adapter_kind: str,
        default_base_url: str,
        created_by: str | None,
        status: str = "active",
    ) -> ProviderDefinitionRow:
        self.upserts.append(
            {
                "provider": provider,
                "display_name": display_name,
                "adapter_kind": adapter_kind,
                "default_base_url": default_base_url,
                "created_by": created_by,
                "status": status,
            }
        )
        row = ProviderDefinitionRow(
            provider=provider,
            display_name=display_name,
            adapter_kind=adapter_kind,
            default_base_url=default_base_url,
            status=status,
            created_at=None,  # type: ignore[arg-type]
            updated_at=None,  # type: ignore[arg-type]
        )
        self.rows[provider] = row
        return row

    async def list_provider_keys(self, provider: str):
        return []

    async def list_provider_keys_full(self, provider: str):
        return []

    async def list_disabled_provider_env_key_hashes(self, provider: str):
        return []

    async def delete_provider_keys_for_provider(self, provider: str) -> int:
        self.deleted_keys.append(provider)
        return 2

    async def delete_provider_definition(self, provider: str) -> bool:
        return self.rows.pop(provider, None) is not None


def _empty_services():
    return SimpleNamespace(router=SimpleNamespace(routes={}))


@pytest.mark.asyncio
async def test_update_builtin_provider_is_rejected(monkeypatch):
    store = FakeProviderDefinitionStore()
    config_spec = provider_definitions.ConfigProviderSpec(
        provider="kimi",
        adapter_kind="kimi",
        default_base_url="https://api.kimi.com/coding/v1",
        model_ids=frozenset(["kimi-test"]),
    )

    async def noop_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        provider_definitions, "_configured_provider_specs", lambda: {"kimi": config_spec}
    )
    monkeypatch.setattr(provider_definitions, "log_admin_action", noop_audit)

    with pytest.raises(HTTPException) as exc_info:
        await provider_definitions.update_provider_definition(
            "kimi",
            UpdateProviderDefinitionRequest(display_name="Kimi Coding"),
            admin_id="admin",
            op_store=store,
            services=_empty_services(),
        )

    assert exc_info.value.status_code == 409
    assert store.upserts == []


@pytest.mark.asyncio
async def test_delete_builtin_provider_is_rejected(monkeypatch):
    store = FakeProviderDefinitionStore()
    config_spec = provider_definitions.ConfigProviderSpec(
        provider="kimi",
        adapter_kind="kimi",
        default_base_url="https://api.kimi.com/coding/v1",
        model_ids=frozenset(),
    )

    async def noop_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        provider_definitions, "_configured_provider_specs", lambda: {"kimi": config_spec}
    )
    monkeypatch.setattr(provider_definitions, "log_admin_action", noop_audit)

    with pytest.raises(HTTPException) as exc_info:
        await provider_definitions.delete_provider_definition(
            "kimi",
            admin_id="admin",
            op_store=store,
            services=_empty_services(),
        )

    assert exc_info.value.status_code == 409
    assert store.upserts == []
    assert store.deleted_keys == []


@pytest.mark.asyncio
async def test_update_custom_provider_display_name_without_probe(monkeypatch):
    store = FakeProviderDefinitionStore(
        {
            "acme": ProviderDefinitionRow(
                provider="acme",
                display_name="Acme",
                adapter_kind="openai_compat",
                default_base_url="https://api.acme.test/v1",
                status="active",
                created_at=None,  # type: ignore[arg-type]
                updated_at=None,  # type: ignore[arg-type]
            )
        }
    )

    async def fail_probe(**_kwargs):
        raise AssertionError("display-name-only edit must not probe")

    async def noop_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(provider_definitions, "_configured_provider_specs", dict)
    monkeypatch.setattr(provider_definitions, "_probe_openai_compat", fail_probe)
    monkeypatch.setattr(provider_definitions, "log_admin_action", noop_audit)

    item = await provider_definitions.update_provider_definition(
        "acme",
        UpdateProviderDefinitionRequest(display_name="Acme Prod"),
        admin_id="admin",
        op_store=store,
        services=_empty_services(),
    )

    assert item.source == "custom"
    assert item.display_name == "Acme Prod"
    assert item.default_base_url == "https://api.acme.test/v1"
    assert store.upserts[0]["status"] == "active"


@pytest.mark.asyncio
async def test_delete_custom_provider_hard_deletes_definition_and_keys(monkeypatch):
    store = FakeProviderDefinitionStore(
        {
            "acme": ProviderDefinitionRow(
                provider="acme",
                display_name="Acme",
                adapter_kind="openai_compat",
                default_base_url="https://api.acme.test/v1",
                status="active",
                created_at=None,  # type: ignore[arg-type]
                updated_at=None,  # type: ignore[arg-type]
            )
        }
    )

    async def noop_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(provider_definitions, "_configured_provider_specs", dict)
    monkeypatch.setattr(provider_definitions, "log_admin_action", noop_audit)

    response = await provider_definitions.delete_provider_definition(
        "acme",
        admin_id="admin",
        op_store=store,
        services=_empty_services(),
    )

    assert response.provider == "acme"
    assert response.deleted_keys == 2
    assert store.deleted_keys == ["acme"]
    assert "acme" not in store.rows
    assert store.upserts == []


def test_configured_provider_specs_include_config_declared_providers(tmp_path, monkeypatch):
    models_config = tmp_path / "models.yaml"
    models_config.write_text(
        """
models:
  - id: kimi-test
    provider: kimi
    route:
      - kind: kimi_coding
        base_url: ${MISSING_KIMI_BASE_URL}
  - id: local-test
    provider: sglang
    route:
      - kind: sglang
        base_url: ${LOCAL_DEPLOYMENT_URL}
  - id: openrouter-test
    provider: openrouter
    route:
      - kind: openrouter[deepinfra]
        base_url: https://openrouter.ai/api/v1
  - id: openai-test
    provider: openai
    route:
      - kind: openai_compat
        base_url: https://example.test/v1
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODELS_CONFIG", str(models_config))
    monkeypatch.setenv("LOCAL_DEPLOYMENT_URL", "http://host.docker.internal:8001/v1")

    specs = _configured_provider_specs()

    assert set(specs) == {"kimi", "openai", "openrouter", "sglang"}
    assert specs["kimi"].default_base_url == "https://api.kimi.com/coding/v1"
    assert specs["kimi"].model_ids == frozenset(["kimi-test"])
    assert specs["sglang"].default_base_url == "http://host.docker.internal:8001/v1"
    assert specs["sglang"].model_ids == frozenset(["local-test"])
    assert specs["openrouter"].adapter_kind == "openrouter"
    assert specs["openrouter"].model_ids == frozenset(["openrouter-test"])
    assert specs["openai"].adapter_kind == "openai_compat"
    assert specs["openai"].model_ids == frozenset(["openai-test"])


def test_configured_provider_specs_fill_local_provider_defaults(tmp_path, monkeypatch):
    models_config = tmp_path / "models.yaml"
    models_config.write_text(
        """
models:
  - id: vllm-test
    provider: vllm
    route:
      - kind: vllm
        base_url: ${SPARK_DEPLOYMENT_URL}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODELS_CONFIG", str(models_config))
    monkeypatch.delenv("SPARK_DEPLOYMENT_URL", raising=False)

    specs = _configured_provider_specs()

    assert specs["vllm"].default_base_url == "http://host.docker.internal:8002/v1"
    assert specs["vllm"].model_ids == frozenset(["vllm-test"])


def test_merge_config_models_by_provider_preserves_runtime_models():
    specs = {
        "kimi": provider_definitions.ConfigProviderSpec(
            provider="kimi",
            adapter_kind="kimi",
            default_base_url="https://api.kimi.com/coding/v1",
            model_ids=frozenset(["kimi-test"]),
        )
    }

    merged = provider_definitions._merge_config_models_by_provider(
        {"kimi": {"runtime-kimi"}, "custom": {"custom-model"}},
        specs,
    )

    assert merged == {
        "kimi": {"runtime-kimi", "kimi-test"},
        "custom": {"custom-model"},
    }
