from types import SimpleNamespace

import pytest

from serving.schemas_admin import UpdateProviderDefinitionRequest
from serving.servers.routers.admin import provider_definitions
from serving.servers.routers.admin.provider_definitions import _configured_provider_specs
from serving.storage.base import ProviderDefinitionRow


class FakeProviderDefinitionStore:
    def __init__(self, rows: dict[str, ProviderDefinitionRow] | None = None):
        self.rows = rows or {}
        self.upserts: list[dict[str, object]] = []

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


def _empty_services():
    return SimpleNamespace(router=SimpleNamespace(routes={}))


@pytest.mark.asyncio
async def test_update_builtin_provider_writes_active_override_without_probe(monkeypatch):
    store = FakeProviderDefinitionStore()
    config_spec = provider_definitions.ConfigProviderSpec(
        provider="kimi",
        adapter_kind="kimi",
        default_base_url="https://api.kimi.com/coding/v1",
    )

    async def clean_base_url(value: str) -> str:
        return value.strip()

    async def fail_probe(**_kwargs):
        raise AssertionError("built-in provider override should not require probe")

    async def noop_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(provider_definitions, "_configured_provider_specs", lambda: {"kimi": config_spec})
    monkeypatch.setattr(provider_definitions, "_validate_base_url", clean_base_url)
    monkeypatch.setattr(provider_definitions, "_probe_openai_compat", fail_probe)
    monkeypatch.setattr(provider_definitions, "log_admin_action", noop_audit)

    item = await provider_definitions.update_provider_definition(
        "kimi",
        UpdateProviderDefinitionRequest(
            display_name="Kimi Coding",
            default_base_url="https://api.kimi.com/coding/v1-alt",
        ),
        admin_id="admin",
        op_store=store,
        services=_empty_services(),
    )

    assert item.source == "built_in"
    assert item.display_name == "Kimi Coding"
    assert item.default_base_url == "https://api.kimi.com/coding/v1-alt"
    assert store.upserts == [
        {
            "provider": "kimi",
            "display_name": "Kimi Coding",
            "adapter_kind": "kimi",
            "default_base_url": "https://api.kimi.com/coding/v1-alt",
            "created_by": "admin",
            "status": "active",
        }
    ]


@pytest.mark.asyncio
async def test_delete_builtin_provider_writes_disabled_marker(monkeypatch):
    store = FakeProviderDefinitionStore()
    config_spec = provider_definitions.ConfigProviderSpec(
        provider="kimi",
        adapter_kind="kimi",
        default_base_url="https://api.kimi.com/coding/v1",
    )

    async def noop_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(provider_definitions, "_configured_provider_specs", lambda: {"kimi": config_spec})
    monkeypatch.setattr(provider_definitions, "log_admin_action", noop_audit)

    response = await provider_definitions.delete_provider_definition(
        "kimi",
        admin_id="admin",
        op_store=store,
        services=_empty_services(),
    )

    assert response.provider == "kimi"
    assert response.deleted_keys == 0
    assert store.upserts == [
        {
            "provider": "kimi",
            "display_name": "Kimi",
            "adapter_kind": "kimi",
            "default_base_url": "https://api.kimi.com/coding/v1",
            "created_by": "admin",
            "status": "disabled",
        }
    ]


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
    assert specs["sglang"].default_base_url == "http://host.docker.internal:8001/v1"
    assert specs["openrouter"].adapter_kind == "openrouter"
    assert specs["openai"].adapter_kind == "openai_compat"


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
