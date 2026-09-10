"""Trusted backend extensions load before consumers and use the shared factory."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from routing.executor import RouteExecutor
from serving import agent_access, extensions
from serving.adapters import ModelConfig, OpenAICompatAdapter, dynamic_keys
from serving.admin import provider_quotas
from serving.admin.provider_key_probe import probe_provider_key_with_existing_route
from serving.schemas_admin import ProviderQuotaResult
from serving.servers import bootstrap, registry
from serving.servers.routers.admin import provider_routes


@pytest.fixture(autouse=True)
def isolated_registries(monkeypatch):
    """Extension registrations are process-global, but tests must not leak them."""
    monkeypatch.delenv("BACKEND_EXTENSIONS", raising=False)
    monkeypatch.setattr(extensions, "_loaded_modules", set())
    monkeypatch.setattr(registry, "ADAPTER_FACTORIES", {})
    monkeypatch.setattr(
        registry, "RESERVED_PROVIDER_LABELS", set(registry.RESERVED_PROVIDER_LABELS)
    )
    dynamic_keys.reset()
    agent_access.reset_agent_access_policy()
    yield
    dynamic_keys.reset()
    agent_access.reset_agent_access_policy()


def _config(provider="example_extension", **extra):
    return {
        "id": "example-model",
        "name": "Example model",
        "provider": provider,
        "base_url": "https://api.example.test/v1",
        "endpoint_id": "example-model:example-api",
        **extra,
    }


def _factory(cfg):
    return OpenAICompatAdapter(ModelConfig(**cfg))


def test_no_extensions_are_loaded_by_default(monkeypatch):
    importer = Mock()
    monkeypatch.setattr(extensions.importlib, "import_module", importer)

    extensions.load_backend_extensions()

    importer.assert_not_called()
    assert registry.ADAPTER_FACTORIES == {}


def test_extensions_import_and_register_once_in_configured_order(monkeypatch):
    first, second = Mock(), Mock()
    importer = Mock(side_effect=[SimpleNamespace(register=first), SimpleNamespace(register=second)])
    monkeypatch.setattr(extensions.importlib, "import_module", importer)
    monkeypatch.setenv("BACKEND_EXTENSIONS", " deployments.first , deployments.second ")

    extensions.load_backend_extensions()
    extensions.load_backend_extensions()

    assert [call.args[0] for call in importer.call_args_list] == [
        "deployments.first",
        "deployments.second",
    ]
    first.assert_called_once_with()
    second.assert_called_once_with()


@pytest.mark.parametrize("configured", ["../module.py", "module:register", "a,a"])
def test_invalid_extension_configuration_fails(monkeypatch, configured):
    monkeypatch.setenv("BACKEND_EXTENSIONS", configured)
    with pytest.raises(RuntimeError):
        extensions.load_backend_extensions()


@pytest.mark.parametrize("failure", [ImportError("missing"), RuntimeError("broken")])
def test_extension_import_failure_aborts_startup(monkeypatch, failure):
    monkeypatch.setenv("BACKEND_EXTENSIONS", "deployment.extension")
    monkeypatch.setattr(extensions.importlib, "import_module", Mock(side_effect=failure))
    with pytest.raises(RuntimeError, match="Failed to load backend extension") as error:
        extensions.load_backend_extensions()
    assert error.value.__cause__ is failure
    assert extensions._loaded_modules == set()


@pytest.mark.parametrize("register", [None, 7, AsyncMock(), Mock(side_effect=ValueError("bad"))])
def test_bad_registration_fails_closed(monkeypatch, register):
    monkeypatch.setenv("BACKEND_EXTENSIONS", "deployment.extension")
    monkeypatch.setattr(
        extensions.importlib, "import_module", Mock(return_value=SimpleNamespace(register=register))
    )
    with pytest.raises(RuntimeError, match="Failed to load backend extension"):
        extensions.load_backend_extensions()
    assert extensions._loaded_modules == set()


@pytest.mark.asyncio
async def test_bootstrap_loads_extensions_after_env_and_before_consumers(monkeypatch):
    events = []

    def load_env():
        events.append("env")
        monkeypatch.setenv("BACKEND_EXTENSIONS", "deployment.extension")

    def register():
        events.append("extension")
        registry.register_adapter_factory("example_extension", _factory)
        agent_access.register_agent_access_policy(lambda _: ["agent.use"])

    def first_consumer():
        events.append("consumer")
        assert "example_extension" in registry.ADAPTER_FACTORIES
        assert agent_access.resolve_agent_access_permissions(user_id="user_1", role="pro") == [
            "agent.use"
        ]
        raise RuntimeError("stop before services start")

    monkeypatch.setattr(bootstrap, "load_dotenv", load_env)
    monkeypatch.setattr(bootstrap, "setup_logging", lambda: None)
    monkeypatch.setattr(bootstrap, "get_settings", first_consumer)
    monkeypatch.setattr(
        extensions.importlib, "import_module", Mock(return_value=SimpleNamespace(register=register))
    )

    with pytest.raises(RuntimeError, match="stop before services start"):
        await bootstrap.initialize()

    assert events == ["env", "extension", "consumer"]


def test_extension_registers_agent_access_only_once(monkeypatch):
    def register():
        agent_access.register_agent_access_policy(lambda _: ["agent.use"])

    monkeypatch.setenv("BACKEND_EXTENSIONS", "deployment.extension")
    monkeypatch.setattr(
        extensions.importlib, "import_module", Mock(return_value=SimpleNamespace(register=register))
    )

    extensions.load_backend_extensions()
    extensions.load_backend_extensions()

    assert agent_access.resolve_agent_access_permissions(user_id="user_1", role="pro") == [
        "agent.use"
    ]


@pytest.mark.asyncio
async def test_bootstrap_does_not_continue_after_extension_failure(monkeypatch):
    consumer = Mock()
    monkeypatch.setattr(bootstrap, "load_dotenv", lambda: None)
    monkeypatch.setattr(bootstrap, "setup_logging", lambda: None)
    monkeypatch.setattr(bootstrap, "get_settings", consumer)
    monkeypatch.setenv("BACKEND_EXTENSIONS", "deployment.missing")
    monkeypatch.setattr(extensions.importlib, "import_module", Mock(side_effect=ImportError))

    with pytest.raises(RuntimeError, match="Failed to load backend extension"):
        await bootstrap.initialize()

    consumer.assert_not_called()


def test_builtin_override_is_explicit_and_logged(caplog):
    factory = Mock(side_effect=_factory)
    with pytest.raises(ValueError, match="requires override=True"):
        registry.register_adapter_factory("zai", factory)
    with caplog.at_level(logging.INFO):
        registry.register_adapter_factory("zai", factory, override=True)
    config = _config("zai")

    adapter = registry._make_adapter("zai", config)

    factory.assert_called_once_with(config)
    assert factory.call_args.args[0] is not config
    assert adapter.config.provider_profile is None
    assert "built-in override: True" in caplog.text


def test_openrouter_override_keeps_pin_without_builtin_profile_defaults():
    factory = Mock(side_effect=_factory)
    registry.register_adapter_factory("openrouter", factory, override=True)
    config = _config("openrouter")

    adapter = registry._make_adapter("openrouter[example]", config)

    factory.assert_called_once_with({**config, "openrouter_pinned_provider": "example"})
    assert adapter.config.provider_profile is None
    assert adapter.config.openrouter_pinned_provider == "example"


def test_wrapped_async_registration_is_rejected(monkeypatch):
    async def async_register():
        pass

    monkeypatch.setenv("BACKEND_EXTENSIONS", "deployment.extension")
    monkeypatch.setattr(
        extensions.importlib,
        "import_module",
        Mock(return_value=SimpleNamespace(register=lambda: async_register())),
    )
    with pytest.raises(RuntimeError, match="Failed to load backend extension"):
        extensions.load_backend_extensions()
    assert extensions._loaded_modules == set()


@pytest.mark.parametrize("override", [False, True])
def test_registered_factory_cannot_be_replaced(override):
    registry.register_adapter_factory("example_extension", _factory)
    with pytest.raises(ValueError, match="already registered"):
        registry.register_adapter_factory("example_extension", _factory, override=override)


@pytest.mark.parametrize("kind", ["", "router", "openai", "not.a.kind", "UPPERCASE", "a" * 65])
def test_invalid_or_synthetic_kinds_cannot_be_registered(kind):
    with pytest.raises(ValueError):
        registry.register_adapter_factory(kind, _factory, override=True)


def test_non_callable_factory_is_rejected():
    with pytest.raises(TypeError, match="must be callable"):
        registry.register_adapter_factory("example_extension", None)


def test_new_kind_reserves_provider_label_without_replacing_set():
    reserved = registry.RESERVED_PROVIDER_LABELS
    registry.register_adapter_factory("example_extension", _factory)

    assert "example_extension" in reserved
    with pytest.raises(ValueError, match="reserved"):
        registry.parse_route_provider_label({"provider": "example_extension"}, "vllm", "m")
    assert registry.parse_route_provider_label({}, "example_extension", "m") == (
        "example_extension",
        None,
    )


@pytest.mark.asyncio
async def test_yaml_adapter_and_key_probe_use_registered_factory(tmp_path, monkeypatch):
    adapters = []

    def factory(cfg):
        adapter = _factory(cfg)
        adapter.chat_completion = AsyncMock(return_value={"id": "example"})
        adapters.append(adapter)
        return adapter

    registry.register_adapter_factory("example_extension", factory)
    monkeypatch.setitem(dynamic_keys._KEY_PROVIDER_ALIASES, "example_extension", "example")
    models = tmp_path / "models.yaml"
    models.write_text(
        "models:\n"
        "  - id: example-model\n"
        "    name: Example model\n"
        "    provider: example\n"
        "    route:\n"
        "      - kind: example_extension\n"
        "        base_url: https://api.example.test/v1\n"
        "        api_keys: [original-key]\n"
    )
    router = RouteExecutor()
    registry.register_from_models_yaml(router, models)
    original = adapters[0]

    await probe_provider_key_with_existing_route(
        SimpleNamespace(router=router), provider="example", api_key="candidate-key"
    )

    assert len(adapters) == 2
    assert adapters[1].config.api_key == "candidate-key"
    assert adapters[1].config.api_keys is None
    assert adapters[1].config.endpoint_id == original.config.endpoint_id
    adapters[1].chat_completion.assert_awaited_once()
    assert original._key_pool.snapshot_keys() == ["original-key"]


@pytest.mark.asyncio
async def test_admin_model_candidate_and_update_paths_use_registered_factory(monkeypatch):
    factory = Mock(side_effect=_factory)
    registry.register_adapter_factory("example_extension", factory)
    dynamic_keys.register_known_provider("example_extension")
    original = _factory(_config())
    router = RouteExecutor()
    router.register_route("example-model", [(original, 1.0)])
    services = SimpleNamespace(router=router, model_router_registry=None)
    monkeypatch.setattr(
        provider_routes, "_validate_base_url", AsyncMock(return_value="https://api.example.test/v1")
    )
    monkeypatch.setattr(
        provider_routes, "_resolve_key_material", AsyncMock(return_value=("candidate-key", None))
    )
    common = {
        "upstream_provider": "example_extension",
        "openrouter_sort": None,
        "base_url": "https://api.example.test/v1",
        "api_key_id": None,
    }
    candidate_args = {
        **common,
        "route_type": "on_demand",
        "provider_model_id": "example-model",
        "quota_limit": None,
        "concurrency_limit": None,
        "weight": 1.0,
    }

    new_model = await provider_routes._prepare_model_route_candidate(
        services,
        None,
        model_id="new-model",
        pricing={"prompt": "0", "completion": "0"},
        strategy="fixed",
        **candidate_args,
    )
    new_route = await provider_routes._prepare_route_candidate(
        services, None, model_id="example-model", **candidate_args
    )
    update = await provider_routes._prepare_route_update(
        services,
        None,
        model_id="example-model",
        route_id=original.config.endpoint_id,
        provider_model_id_override="example-model",
        **common,
    )

    assert factory.call_count == 3
    for prepared in (new_model, new_route, update):
        assert prepared.adapter.config.provider == "example_extension"
        assert prepared.adapter.config.api_key == "candidate-key"


@pytest.mark.asyncio
async def test_extension_registers_the_quota_fetchers_the_tab_reports_on():
    """The gateway ships no quota fetchers; an extension supplies them."""
    provider_quotas.reset_quota_fetchers()
    try:
        assert await provider_quotas.gather_all() == []

        async def fetch(operational_store=None, services=None):
            return [
                ProviderQuotaResult(
                    name="example",
                    display_name="Example",
                    key_configured=True,
                    key_masked="exampl...1234",
                    fetched_at=datetime.now(timezone.utc),
                    ok=True,
                    error=None,
                    usages=[],
                )
            ]

        provider_quotas.register_quota_fetcher("example", "Example", fetch)

        results = await provider_quotas.gather_all()
        assert [(r.name, r.display_name) for r in results] == [("example", "Example")]
        # RouteWise quota sources resolve through the same registry.
        assert provider_quotas.quota_fetcher("example").fetch is fetch
        with pytest.raises(ValueError, match="already registered"):
            provider_quotas.register_quota_fetcher("example", "Example", fetch)
    finally:
        provider_quotas.reset_quota_fetchers()
