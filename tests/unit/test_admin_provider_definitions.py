from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from serving.adapters import provider_registry
from serving.schemas_admin import CreateProviderDefinitionRequest, UpdateProviderDefinitionRequest
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

    async def list_provider_definitions(self):
        return list(self.rows.values())

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


def _services_with_runtime_provider(provider: str, model_id: str = "runtime-model"):
    adapter = SimpleNamespace(
        config=SimpleNamespace(
            id=model_id,
            provider=provider,
            route_metadata={},
        )
    )
    route = SimpleNamespace(raw_adapters=[(adapter, 1.0, "runtime-route")])
    return SimpleNamespace(router=SimpleNamespace(routes={model_id: route}))


@pytest.mark.asyncio
async def test_update_builtin_provider_is_rejected(monkeypatch):
    store = FakeProviderDefinitionStore()
    config_spec = provider_definitions.ConfigProviderSpec(
        provider="kimi",
        adapter_kind="kimi",
        default_base_url="https://api.moonshot.ai/v1",
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
        default_base_url="https://api.moonshot.ai/v1",
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
async def test_create_custom_provider_rejects_blank_display_name(monkeypatch):
    store = FakeProviderDefinitionStore()

    async def fail_probe(**_kwargs):
        raise AssertionError("blank display name must fail before probing")

    monkeypatch.setattr(provider_definitions, "_configured_provider_specs", dict)
    monkeypatch.setattr(provider_definitions, "_probe_openai_compat", fail_probe)

    with pytest.raises(HTTPException) as exc_info:
        await provider_definitions.create_provider_definition(
            CreateProviderDefinitionRequest(
                provider="acme",
                display_name="   ",
                adapter_kind="openai_compat",
                default_base_url="https://api.acme.test/v1",
                api_key="sk-test",
                probe_model_id="acme/model",
            ),
            admin_id="admin",
            op_store=store,
            services=_empty_services(),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "display_name must not be blank"
    assert store.upserts == []


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


@pytest.mark.asyncio
async def test_list_provider_definitions_omits_unconfigured_selectable_providers(monkeypatch):
    store = FakeProviderDefinitionStore()

    monkeypatch.setattr(provider_definitions, "_configured_provider_specs", dict)
    monkeypatch.setattr(provider_definitions.dynamic_keys, "get_known_providers", set)

    response = await provider_definitions.list_provider_definitions(
        _admin_id="admin",
        op_store=store,
        services=_empty_services(),
    )

    providers = {row.provider for row in response.providers}
    assert providers.isdisjoint(provider_definitions.PROVIDER_TARGETS)
    assert providers == set()


@pytest.mark.asyncio
async def test_list_provider_definitions_includes_runtime_provider(monkeypatch):
    store = FakeProviderDefinitionStore()

    monkeypatch.setattr(provider_definitions, "_configured_provider_specs", dict)
    monkeypatch.setattr(
        provider_definitions.dynamic_keys,
        "get_known_providers",
        lambda: {"featherless"},
    )

    response = await provider_definitions.list_provider_definitions(
        _admin_id="admin",
        op_store=store,
        services=_services_with_runtime_provider("chutes"),
    )

    assert [row.provider for row in response.providers] == ["chutes"]


@pytest.mark.asyncio
async def test_list_provider_definitions_omits_stale_runtime_provider(monkeypatch):
    store = FakeProviderDefinitionStore()

    monkeypatch.setattr(provider_definitions, "_configured_provider_specs", dict)
    monkeypatch.setattr(
        provider_definitions.dynamic_keys,
        "get_known_providers",
        lambda: {"chutes"},
    )

    response = await provider_definitions.list_provider_definitions(
        _admin_id="admin",
        op_store=store,
        services=_empty_services(),
    )

    assert response.providers == []


@pytest.mark.asyncio
async def test_list_provider_definitions_excludes_openrouter_pinned_route_targets(monkeypatch):
    store = FakeProviderDefinitionStore()
    openrouter_spec = provider_definitions.ConfigProviderSpec(
        provider="openrouter",
        adapter_kind="openrouter",
        default_base_url="https://openrouter.ai/api/v1",
        model_ids=frozenset(["openrouter-test"]),
    )

    monkeypatch.setattr(
        provider_definitions,
        "_configured_provider_specs",
        lambda: {"openrouter": openrouter_spec},
    )
    monkeypatch.setattr(provider_definitions.dynamic_keys, "get_known_providers", set)

    response = await provider_definitions.list_provider_definitions(
        _admin_id="admin",
        op_store=store,
        services=_empty_services(),
    )

    providers = {row.provider for row in response.providers}
    assert "openrouter" in providers
    assert "deepinfra" not in providers
    assert "parasail" not in providers


@pytest.mark.asyncio
async def test_list_provider_definitions_ignores_legacy_builtin_definition_row(monkeypatch):
    store = FakeProviderDefinitionStore(
        {
            "chutes": ProviderDefinitionRow(
                provider="chutes",
                display_name="Legacy Chutes",
                adapter_kind="openai_compat",
                default_base_url="https://legacy-chutes.test/v1",
                status="active",
                created_at=None,  # type: ignore[arg-type]
                updated_at=None,  # type: ignore[arg-type]
            )
        }
    )
    registered_rows = []

    monkeypatch.setattr(provider_definitions, "_configured_provider_specs", dict)
    monkeypatch.setattr(provider_definitions.dynamic_keys, "get_known_providers", set)
    monkeypatch.setattr(
        provider_definitions.provider_registry,
        "register_provider_definition",
        registered_rows.append,
    )

    response = await provider_definitions.list_provider_definitions(
        _admin_id="admin",
        op_store=store,
        services=_empty_services(),
    )

    assert response.providers == []
    assert registered_rows == []


def test_configured_provider_specs_include_config_declared_providers(tmp_path, monkeypatch):
    models_config = tmp_path / "models.yaml"
    models_config.write_text(
        """
models:
  - id: kimi-test
    provider: kimi
    route:
      - kind: kimi
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
    assert specs["kimi"].default_base_url == "https://api.moonshot.ai/v1"
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
            default_base_url="https://api.moonshot.ai/v1",
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


@pytest.mark.asyncio
async def test_provider_registry_boot_skips_reserved_provider_rows():
    now = datetime.now(timezone.utc)

    class Store:
        async def list_provider_definitions(self):
            return [
                ProviderDefinitionRow(
                    provider="kimi",
                    display_name="Kimi Override",
                    adapter_kind="openai_compat",
                    default_base_url="https://api.kimi-override.test/v1",
                    status="active",
                    created_at=now,
                    updated_at=now,
                ),
                ProviderDefinitionRow(
                    provider="acme",
                    display_name="Acme",
                    adapter_kind="openai_compat",
                    default_base_url="https://api.acme.test/v1",
                    status="active",
                    created_at=now,
                    updated_at=now,
                ),
            ]

    provider_registry.unregister_provider_definition("kimi")
    provider_registry.unregister_provider_definition("acme")
    try:
        await provider_registry.apply_provider_definitions_at_boot(
            Store(),
            reserved_providers={"kimi"},
        )

        assert provider_registry.get_provider_definition("kimi") is None
        assert provider_registry.get_provider_definition("acme") is not None
    finally:
        provider_registry.unregister_provider_definition("kimi")
        provider_registry.unregister_provider_definition("acme")


class _NoKeysStore:
    """Minimal op_store for _build_provider_item: no keys of any kind."""

    async def list_provider_keys(self, provider: str):
        return []

    async def list_provider_keys_full(self, provider: str):
        return []

    async def list_disabled_provider_env_key_hashes(self, provider: str):
        return set()


def test_configured_provider_specs_pick_up_route_labels_and_display_names(tmp_path, monkeypatch):
    """Route-level `provider:` becomes its own registry slug, and its
    `provider_display_name:` is the name the tab shows."""
    models_config = tmp_path / "models.yaml"
    models_config.write_text(
        """
models:
  - id: qwen-local
    provider: vllm
    route:
      - kind: vllm
        provider: local-a
        provider_display_name: "Local box A"
        base_url: http://localhost:8002/v1
      - kind: vllm
        provider: local-b
        provider_display_name: "Local box B"
        base_url: http://localhost:8003/v1
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODELS_CONFIG", str(models_config))

    specs = _configured_provider_specs()

    assert {"local-a", "local-b"} <= set(specs)
    assert specs["local-a"].display_name == "Local box A"
    assert specs["local-b"].display_name == "Local box B"
    # The adapter kind still comes from `kind:`, not the label.
    assert specs["local-a"].adapter_kind == "vllm"


@pytest.mark.asyncio
async def test_display_name_applies_to_a_selectable_built_in_provider(monkeypatch):
    """A `provider_display_name:` on a route of a PROVIDER_TARGETS provider must
    reach the registry tab too, or it would disagree with the performance,
    token-usage, and availability views, which read the same config name."""
    spec = provider_definitions.ConfigProviderSpec(
        provider="chutes",
        adapter_kind="chutes",
        default_base_url="https://llm.chutes.ai/v1",
        model_ids=frozenset(["minimax-fast"]),
        display_name="Chutes (subscription)",
    )
    op_store = _NoKeysStore()

    item = await provider_definitions._build_provider_item(
        provider="chutes",
        custom_row=None,
        config_spec=spec,
        source="built_in",
        op_store=op_store,
        models_by_provider={"chutes": {"minimax-fast"}},
    )

    assert item.display_name == "Chutes (subscription)"


@pytest.mark.asyncio
async def test_built_in_label_is_used_when_config_names_nothing(monkeypatch):
    spec = provider_definitions.ConfigProviderSpec(
        provider="chutes",
        adapter_kind="chutes",
        default_base_url="https://llm.chutes.ai/v1",
        model_ids=frozenset(["minimax-fast"]),
    )
    op_store = _NoKeysStore()

    item = await provider_definitions._build_provider_item(
        provider="chutes",
        custom_row=None,
        config_spec=spec,
        source="built_in",
        op_store=op_store,
        models_by_provider={"chutes": {"minimax-fast"}},
    )

    assert item.display_name == "Chutes"


@pytest.mark.asyncio
async def test_boot_keeps_a_custom_provider_a_route_label_collides_with(caplog):
    """Creating a custom provider whose slug matches a route label is already
    rejected, so a collision means the label was added afterwards. The custom
    definition owns a key pool and a route target that live routes depend on,
    so it must survive — loudly, since both now report under one label."""
    now = datetime.now(timezone.utc)

    class Store:
        async def list_provider_definitions(self):
            return [
                ProviderDefinitionRow(
                    provider="local-a",
                    display_name="Acme Local",
                    adapter_kind="openai_compat",
                    default_base_url="https://api.acme.test/v1",
                    status="active",
                    created_at=now,
                    updated_at=now,
                ),
            ]

    provider_registry.unregister_provider_definition("local-a")
    try:
        with caplog.at_level("ERROR"):
            await provider_registry.apply_provider_definitions_at_boot(
                Store(),
                # The label is config-managed, so it also appears in reserved.
                reserved_providers={"local-a", "vllm"},
                config_label_providers={"local-a"},
            )

        assert provider_registry.get_provider_definition("local-a") is not None
        assert "local-a" in caplog.text
    finally:
        provider_registry.unregister_provider_definition("local-a")


def test_config_route_provider_labels_lists_only_label_slugs(tmp_path, monkeypatch):
    models_config = tmp_path / "models.yaml"
    models_config.write_text(
        """
models:
  - id: qwen-local
    provider: vllm
    route:
      - kind: vllm
        provider: local-a
        base_url: http://localhost:8002/v1
      - kind: vllm
        base_url: http://localhost:8003/v1
  - id: minimax-fast
    provider: minimax
    route:
      - kind: chutes
        base_url: https://llm.chutes.ai/v1
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODELS_CONFIG", str(models_config))

    labels = provider_definitions.config_route_provider_labels()

    # Only the relabelled slug — not the kinds, and not providers the gateway
    # can build on its own.
    assert labels == {"local-a"}


def test_a_label_spelled_as_its_own_kind_is_not_label_only(tmp_path, monkeypatch):
    """`provider: vllm` on a `kind: vllm` route is a no-op, so the slug stays a
    real built-in and a stored row for it must still be skipped at boot."""
    models_config = tmp_path / "models.yaml"
    models_config.write_text(
        """
models:
  - id: qwen-local
    provider: vllm
    route:
      - kind: vllm
        provider: vllm
        base_url: http://localhost:8002/v1
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODELS_CONFIG", str(models_config))

    assert provider_definitions.config_route_provider_labels() == set()


def _services_with_runtime_routes(*adapters):
    routes = {}
    for index, (model_id, provider, route_metadata, pinned) in enumerate(adapters):
        adapter = SimpleNamespace(
            config=SimpleNamespace(
                id=model_id,
                provider=provider,
                route_metadata=route_metadata,
                openrouter_pinned_provider=pinned,
            )
        )
        routes[model_id] = SimpleNamespace(raw_adapters=[(adapter, 1.0, f"runtime-route-{index}")])
    return SimpleNamespace(router=SimpleNamespace(routes=routes))


@pytest.mark.asyncio
async def test_list_provider_definitions_credits_pinned_runtime_routes_to_openrouter(
    monkeypatch,
):
    """A pinned OpenRouter route is an OpenRouter route, not a ``parasail`` provider.

    The Routing tab stores the bare target name (``parasail``, ``deepinfra``)
    as the route's upstream while ``config.provider`` stays ``openrouter``.
    Listing every label attached to a live route materialized ghost rows with
    no keys and no base URL for what are routing preferences. The registry row
    is the credential owner: OpenRouter for a pin, the key-pool provider for a
    relabelled route.
    """
    store = FakeProviderDefinitionStore()

    monkeypatch.setattr(provider_definitions, "_configured_provider_specs", dict)
    monkeypatch.setattr(provider_definitions.dynamic_keys, "get_known_providers", set)

    response = await provider_definitions.list_provider_definitions(
        _admin_id="admin",
        op_store=store,
        services=_services_with_runtime_routes(
            (
                "minimax-fast",
                "openrouter",
                {
                    "route_provider": "parasail",
                    "upstream_provider": "parasail",
                    "provider_type": "quota",
                },
                "parasail",
            ),
            (
                "minimax-m2.5",
                "openrouter",
                {"route_provider": "deepinfra", "upstream_provider": "deepinfra"},
                "deepinfra",
            ),
            (
                "local-chat",
                "gpu-a",
                {"key_provider": "vllm", "upstream_provider": "vllm"},
                None,
            ),
        ),
    )

    providers = {row.provider: row for row in response.providers}
    assert set(providers) == {"openrouter", "vllm"}
    assert providers["openrouter"].models_count == 2
    assert providers["openrouter"].default_base_url


@pytest.mark.asyncio
async def test_a_route_that_declares_its_upstream_lists_only_that_upstream(tmp_path, monkeypatch):
    """A DeepSeek model served by a self-hosted endpoint is one provider, not two.

    The model and its route both say ``deepseek`` (the public identity) while
    ``route_metadata`` names the endpoint's real owner. Listing ``deepseek``
    too showed an undeletable row with no keys and the public DeepSeek URL.
    """
    models_config = tmp_path / "models.yaml"
    models_config.write_text(
        """
models:
  - id: deepseek-v4.1-flash
    provider: deepseek
    route:
      - kind: deepseek
        provider: deepseek
        provider_display_name: DeepSeek
        route_metadata:
          key_provider: gpu_cluster
          route_provider: gpu_cluster
          upstream_provider: gpu_cluster
        base_url: http://gpu-cluster.internal:8000/v1
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODELS_CONFIG", str(models_config))
    monkeypatch.setattr(provider_definitions.dynamic_keys, "get_known_providers", set)

    specs = _configured_provider_specs()
    assert provider_definitions._registry_provider_names(specs) == set()
    assert "deepseek" in provider_definitions._config_managed_provider_names(specs)

    response = await provider_definitions.list_provider_definitions(
        _admin_id="admin",
        op_store=FakeProviderDefinitionStore(),
        services=_services_with_runtime_routes(
            (
                "deepseek-v4.1-flash",
                "deepseek",
                {"key_provider": "gpu_cluster", "upstream_provider": "gpu_cluster"},
                None,
            ),
        ),
    )

    providers = {row.provider: row for row in response.providers}
    assert set(providers) == {"gpu_cluster"}
    assert providers["gpu_cluster"].models_count == 1


def test_a_model_level_provider_is_not_a_registry_row(tmp_path, monkeypatch):
    """`provider:` on the model names its brand; the routes name who serves it.

    A Kimi model served through the coding endpoint and Ollama never calls the
    Moonshot API, so a `kimi` row would point at a URL no route uses.
    """
    models_config = tmp_path / "models.yaml"
    models_config.write_text(
        """
models:
  - id: kimi-code
    provider: kimi
    route:
      - kind: kimi_coding
        base_url: https://api.kimi.com/coding/v1
      - kind: ollama
        base_url: https://ollama.com/v1
  - id: glm-test
    provider: zai
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODELS_CONFIG", str(models_config))

    specs = _configured_provider_specs()

    assert provider_definitions._registry_provider_names(specs) == {"kimi_coding", "ollama", "zai"}
    assert specs["zai"].model_ids == frozenset(["glm-test"])
    assert "kimi" in provider_definitions._config_managed_provider_names(specs)


def test_a_model_whose_routes_are_all_relabelled_lists_only_the_labels(tmp_path, monkeypatch):
    models_config = tmp_path / "models.yaml"
    models_config.write_text(
        """
models:
  - id: quota-demo
    provider: openai_compat
    route:
      - kind: openai_compat
        provider: example_fallback
        base_url: http://127.0.0.1:18352/v1
      - kind: openai_compat
        provider: example_quota
        base_url: http://127.0.0.1:18353/v1
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODELS_CONFIG", str(models_config))

    specs = _configured_provider_specs()

    assert provider_definitions._registry_provider_names(specs) == {
        "example_fallback",
        "example_quota",
    }


def test_a_route_base_url_wins_over_the_model_provider_default(tmp_path, monkeypatch):
    models_config = tmp_path / "models.yaml"
    models_config.write_text(
        """
models:
  - id: qwen-local
    provider: vllm
    route:
      - kind: vllm
        base_url: http://vllm-box.internal:8002/v1
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODELS_CONFIG", str(models_config))

    specs = _configured_provider_specs()

    assert specs["vllm"].listed
    assert specs["vllm"].default_base_url == "http://vllm-box.internal:8002/v1"
    assert specs["vllm"].model_ids == frozenset(["qwen-local"])


@pytest.mark.asyncio
async def test_create_provider_definition_rejects_key_provider_aliases(monkeypatch):
    """A slug the key registry folds into another provider cannot be custom.

    Extension aliases normalize to the same provider everywhere keys and
    routes are grouped, so a custom provider with that slug is unsafe.
    """
    store = FakeProviderDefinitionStore()
    monkeypatch.setitem(
        provider_definitions.dynamic_keys._KEY_PROVIDER_ALIASES, "extension_alias", "example"
    )

    monkeypatch.setattr(provider_definitions, "_configured_provider_specs", dict)
    monkeypatch.setattr(provider_definitions.dynamic_keys, "get_known_providers", set)

    with pytest.raises(HTTPException) as excinfo:
        await provider_definitions.create_provider_definition(
            CreateProviderDefinitionRequest(
                provider="extension_alias",
                display_name="Extension Alias",
                adapter_kind="openai_compat",
                default_base_url="https://api.example.test/v1",
                api_key="secret-key-1234567890",
                probe_model_id="example-chat",
            ),
            admin_id="admin",
            op_store=store,
            services=_empty_services(),
        )

    assert excinfo.value.status_code == 409
    assert store.upserts == []


@pytest.mark.asyncio
async def test_list_provider_definitions_ignores_alias_named_definition_row(monkeypatch):
    monkeypatch.setitem(
        provider_definitions.dynamic_keys._KEY_PROVIDER_ALIASES, "extension_alias", "example"
    )
    store = FakeProviderDefinitionStore(
        {
            "extension_alias": ProviderDefinitionRow(
                provider="extension_alias",
                display_name="Legacy Extension",
                adapter_kind="openai_compat",
                default_base_url="https://api.example.test/v1",
                status="active",
                created_at=None,  # type: ignore[arg-type]
                updated_at=None,  # type: ignore[arg-type]
            )
        }
    )
    registered_rows = []

    monkeypatch.setattr(provider_definitions, "_configured_provider_specs", dict)
    monkeypatch.setattr(provider_definitions.dynamic_keys, "get_known_providers", set)
    monkeypatch.setattr(
        provider_definitions.provider_registry,
        "register_provider_definition",
        registered_rows.append,
    )

    response = await provider_definitions.list_provider_definitions(
        _admin_id="admin",
        op_store=store,
        services=_empty_services(),
    )

    assert response.providers == []
    assert registered_rows == []


def test_configured_provider_specs_resolve_a_registered_default(tmp_path, monkeypatch):
    """The base URL shown is the one the model registry now resolves the route to."""
    models_config = tmp_path / "models.yaml"
    models_config.write_text(
        """
models:
  - id: chutes-test
    route:
      - kind: chutes
        base_url: ${CHUTES_BASE_URL}
  - id: minimax-test
    route:
      - kind: minimax
        base_url: ${MINIMAX_BASE_URL:-https://inline.minimax.example/v1}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODELS_CONFIG", str(models_config))
    monkeypatch.delenv("CHUTES_BASE_URL", raising=False)
    monkeypatch.delenv("MINIMAX_BASE_URL", raising=False)

    specs = _configured_provider_specs()

    assert specs["chutes"].default_base_url == "https://llm.chutes.ai/v1"
    # An inline default still wins over the registered one.
    assert specs["minimax"].default_base_url == "https://inline.minimax.example/v1"

    monkeypatch.setenv("CHUTES_BASE_URL", "https://chutes.internal/v1")
    assert _configured_provider_specs()["chutes"].default_base_url == "https://chutes.internal/v1"


# ---------------------------------------------------------------------------
# Create / update / delete: the paths a successful probe leads to
# ---------------------------------------------------------------------------

_KEY = "sk-acme-secret-1234567890"


def _acme_row(base_url: str = "https://api.acme.test/v1") -> ProviderDefinitionRow:
    return ProviderDefinitionRow(
        provider="acme",
        display_name="Acme",
        adapter_kind="openai_compat",
        default_base_url=base_url,
        status="active",
        created_at=None,  # type: ignore[arg-type]
        updated_at=None,  # type: ignore[arg-type]
    )


class _KeyStore(FakeProviderDefinitionStore):
    def __init__(self, rows=None, *, key_error: Exception | None = None):
        super().__init__(rows)
        self.keys: list[dict[str, object]] = []
        self.key_error = key_error

    async def add_provider_key(self, *, provider, api_key, label, created_by):
        if self.key_error is not None:
            raise self.key_error
        self.keys.append({"provider": provider, "api_key": api_key, "label": label})
        return "key-1"


@pytest.fixture
def wiring(monkeypatch):
    """Record what a write registers, probes and audits, without global state."""
    seen: dict[str, list] = {"probes": [], "registered": [], "pooled": [], "audits": []}

    async def probe(**kwargs):
        seen["probes"].append(kwargs)

    async def base_url(value: str) -> str:
        return value.rstrip("/")

    async def audit(_store, _admin, action, _target, details):
        seen["audits"].append((action, details))

    async def load_roles(_store, _provider):
        return None

    monkeypatch.setattr(provider_definitions, "_probe_openai_compat", probe)
    monkeypatch.setattr(provider_definitions, "_validate_base_url", base_url)
    monkeypatch.setattr(provider_definitions, "log_admin_action", audit)
    monkeypatch.setattr(provider_definitions, "_configured_provider_specs", dict)
    monkeypatch.setattr(provider_definitions.dynamic_keys, "get_known_providers", set)
    monkeypatch.setattr(
        provider_definitions.dynamic_keys, "load_min_role_declarations_for_provider", load_roles
    )
    monkeypatch.setattr(
        provider_definitions.dynamic_keys,
        "add_key_to_provider",
        lambda provider, key: seen["pooled"].append((provider, key)),
    )
    monkeypatch.setattr(
        provider_registry, "register_provider_definition", seen["registered"].append
    )
    return seen


def _create_request(**overrides) -> CreateProviderDefinitionRequest:
    return CreateProviderDefinitionRequest(
        **{
            "provider": "acme",
            "display_name": " Acme ",
            "adapter_kind": "openai_compat",
            "default_base_url": "https://api.acme.test/v1/",
            "api_key": f"  {_KEY}  ",
            "probe_model_id": "acme/model",
            **overrides,
        }
    )


@pytest.mark.asyncio
async def test_create_probes_then_stores_registers_and_pools_the_key(wiring):
    store = _KeyStore()

    item = await provider_definitions.create_provider_definition(
        _create_request(), admin_id="admin", op_store=store, services=_empty_services()
    )

    assert item.provider == "acme" and item.source == "custom"
    [probe] = wiring["probes"]
    assert probe["default_base_url"] == "https://api.acme.test/v1"
    assert store.rows["acme"].display_name == "Acme"
    assert store.keys == [{"provider": "acme", "api_key": _KEY, "label": "Initial key"}]
    assert [row.provider for row in wiring["registered"]] == ["acme"]
    assert wiring["pooled"] == [("acme", _KEY)]
    [(action, details)] = wiring["audits"]
    assert action == "provider_definition.create"
    # The audit names the key by its row id, never by its value.
    assert details["provider_key_id"] == "key-1"
    assert _KEY not in repr(details)


@pytest.mark.asyncio
async def test_a_key_that_cannot_be_stored_leaves_no_keyless_provider(wiring):
    store = _KeyStore(key_error=RuntimeError("key table unavailable"))

    with pytest.raises(RuntimeError, match="key table unavailable"):
        await provider_definitions.create_provider_definition(
            _create_request(), admin_id="admin", op_store=store, services=_empty_services()
        )

    assert "acme" not in store.rows
    assert wiring["registered"] == [] and wiring["pooled"] == [] and wiring["audits"] == []


@pytest.mark.asyncio
async def test_a_failed_probe_writes_nothing(wiring, monkeypatch):
    async def refuse(**_kwargs):
        raise HTTPException(status_code=400, detail="Authentication failed. Check the API key.")

    monkeypatch.setattr(provider_definitions, "_probe_openai_compat", refuse)
    store = _KeyStore()

    with pytest.raises(HTTPException) as caught:
        await provider_definitions.create_provider_definition(
            _create_request(), admin_id="admin", op_store=store, services=_empty_services()
        )

    assert caught.value.status_code == 400
    assert store.rows == {} and store.keys == [] and wiring["registered"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "status"),
    [
        ("existing-definition", 409),
        ("known-runtime-provider", 409),
        ("unsupported-adapter", 422),
    ],
)
async def test_create_refuses_before_probing(wiring, monkeypatch, setup, status):
    store = _KeyStore({"acme": _acme_row()} if setup == "existing-definition" else None)
    if setup == "known-runtime-provider":
        monkeypatch.setattr(
            provider_definitions.dynamic_keys, "get_known_providers", lambda: {"acme"}
        )
    request = _create_request()
    if setup == "unsupported-adapter":
        request = request.model_copy(update={"adapter_kind": "anthropic"})

    with pytest.raises(HTTPException) as caught:
        await provider_definitions.create_provider_definition(
            request, admin_id="admin", op_store=store, services=_empty_services()
        )

    assert caught.value.status_code == status
    assert wiring["probes"] == [] and store.keys == []


@pytest.mark.asyncio
async def test_a_base_url_change_needs_a_key_and_model_to_probe(wiring):
    store = _KeyStore({"acme": _acme_row()})

    with pytest.raises(HTTPException) as caught:
        await provider_definitions.update_provider_definition(
            "acme",
            UpdateProviderDefinitionRequest(default_base_url="https://new.acme.test/v1"),
            admin_id="admin",
            op_store=store,
            services=_empty_services(),
        )

    assert caught.value.status_code == 422
    assert wiring["probes"] == [] and store.upserts == []


@pytest.mark.asyncio
async def test_a_base_url_change_is_probed_before_it_is_saved(wiring):
    store = _KeyStore({"acme": _acme_row()})

    item = await provider_definitions.update_provider_definition(
        "acme",
        UpdateProviderDefinitionRequest(
            default_base_url="https://new.acme.test/v1/",
            api_key=_KEY,
            probe_model_id="acme/model",
        ),
        admin_id="admin",
        op_store=store,
        services=_empty_services(),
    )

    [probe] = wiring["probes"]
    assert probe["default_base_url"] == "https://new.acme.test/v1"
    assert item.default_base_url == "https://new.acme.test/v1"
    [(action, details)] = wiring["audits"]
    assert action == "provider_definition.update" and details["base_url_changed"] is True
    assert _KEY not in repr(details)


@pytest.mark.asyncio
async def test_the_same_base_url_is_not_a_change_and_is_not_probed(wiring):
    store = _KeyStore({"acme": _acme_row()})

    await provider_definitions.update_provider_definition(
        "acme",
        UpdateProviderDefinitionRequest(default_base_url="https://api.acme.test/v1/"),
        admin_id="admin",
        op_store=store,
        services=_empty_services(),
    )

    assert wiring["probes"] == []
    assert wiring["audits"][0][1]["base_url_changed"] is False


@pytest.mark.asyncio
async def test_a_provider_models_route_to_cannot_move_or_go(wiring):
    """Moving or deleting it would silently break the models routed to it."""
    store = _KeyStore({"acme": _acme_row()})
    services = _services_with_runtime_provider("acme")

    with pytest.raises(HTTPException) as moved:
        await provider_definitions.update_provider_definition(
            "acme",
            UpdateProviderDefinitionRequest(
                default_base_url="https://new.acme.test/v1", api_key=_KEY, probe_model_id="m"
            ),
            admin_id="admin",
            op_store=store,
            services=services,
        )
    with pytest.raises(HTTPException) as deleted:
        await provider_definitions.delete_provider_definition(
            "acme", admin_id="admin", op_store=store, services=services
        )

    assert (moved.value.status_code, deleted.value.status_code) == (409, 409)
    assert wiring["probes"] == [] and store.upserts == []
    assert "acme" in store.rows and store.deleted_keys == []


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["update", "delete"])
async def test_an_unknown_provider_is_404(wiring, call):
    store = _KeyStore()

    with pytest.raises(HTTPException) as caught:
        if call == "update":
            await provider_definitions.update_provider_definition(
                "acme",
                UpdateProviderDefinitionRequest(display_name="x"),
                admin_id="admin",
                op_store=store,
                services=_empty_services(),
            )
        else:
            await provider_definitions.delete_provider_definition(
                "acme", admin_id="admin", op_store=store, services=_empty_services()
            )

    assert caught.value.status_code == 404
