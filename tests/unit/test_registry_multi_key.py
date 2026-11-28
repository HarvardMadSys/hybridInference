"""Loader tests for `api_keys` multi-key route entries."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest
import yaml

from routing.routers import FixedRouter
from serving.adapters import dynamic_keys
from serving.servers.registry import (
    ModelLoadReport,
    register_from_models_document,
    register_from_models_yaml,
    route_key_provider,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(textwrap.dedent(body))
    return path


def test_route_level_input_modalities_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIMARY_KEY", "k1")
    monkeypatch.setenv("FALLBACK_KEY", "k2")

    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: vmodel
            name: vmodel
            provider: openai_compat
            base_url: https://primary.example.com
            input_modalities: ["text", "image", "video"]
            route:
              - kind: openai_compat
                weight: 1.0
                base_url: https://primary.example.com
                api_key: ${PRIMARY_KEY}
              - kind: ollama
                weight: 0.5
                base_url: https://fallback.example.com
                api_key: ${FALLBACK_KEY}
                input_modalities: ["text"]
        """,
    )

    router = FixedRouter()
    register_from_models_yaml(router, yaml_path)

    adapters = router.routes["vmodel"].adapters
    assert len(adapters) == 2
    # Primary inherits the model-level union; the fallback override narrows it.
    assert set(adapters[0][0].config.input_modalities) == {"text", "image", "video"}
    assert adapters[1][0].config.input_modalities == ["text"]


def test_api_keys_list_is_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY_1", "key-one")
    monkeypatch.setenv("ZAI_API_KEY_2", "key-two")

    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: glm-test
            name: glm-test
            provider: zai
            base_url: https://api.example.com
            route:
              - kind: zai
                weight: 1.0
                base_url: https://api.example.com
                api_keys:
                  - ${ZAI_API_KEY_1}
                  - ${ZAI_API_KEY_2}
        """,
    )

    router = FixedRouter()
    register_from_models_yaml(router, yaml_path)

    # In this codebase, RouteConfig.adapters is a list of (adapter, weight)
    # tuples, so we index [0][0] to reach the adapter instance.
    adapters = router.routes["glm-test"].adapters
    assert len(adapters) == 1
    cfg = adapters[0][0].config
    assert cfg.api_keys == ["key-one", "key-two"]
    assert cfg.api_key is None


def test_api_key_and_api_keys_both_set_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY_1", "k1")
    monkeypatch.setenv("ZAI_API_KEY_OTHER", "k2")

    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: bad
            name: bad
            provider: zai
            base_url: https://api.example.com
            route:
              - kind: zai
                weight: 1.0
                base_url: https://api.example.com
                api_key: ${ZAI_API_KEY_OTHER}
                api_keys:
                  - ${ZAI_API_KEY_1}
        """,
    )

    router = FixedRouter()
    with pytest.raises(ValueError, match=r"api_key.*api_keys"):
        register_from_models_yaml(router, yaml_path)


def test_blank_api_keys_are_dropped_with_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ZAI_API_KEY_1", "live-key")
    monkeypatch.delenv("ZAI_API_KEY_2", raising=False)
    # ZAI_API_KEY_2 intentionally unset

    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: glm-test
            name: glm-test
            provider: zai
            base_url: https://api.example.com
            route:
              - kind: zai
                weight: 1.0
                base_url: https://api.example.com
                api_keys:
                  - ${ZAI_API_KEY_1}
                  - ${ZAI_API_KEY_2}
        """,
    )

    router = FixedRouter()
    with caplog.at_level("WARNING"):
        register_from_models_yaml(router, yaml_path)
    assert "ZAI_API_KEY_2" in caplog.text or "blank" in caplog.text.lower()
    cfg = router.routes["glm-test"].adapters[0][0].config
    assert cfg.api_keys == ["live-key"]


def test_all_api_keys_blank_raises(tmp_path, monkeypatch):
    # Neither env var set
    monkeypatch.delenv("MISSING_1", raising=False)
    monkeypatch.delenv("MISSING_2", raising=False)
    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: glm-test
            name: glm-test
            provider: zai
            base_url: https://api.example.com
            route:
              - kind: zai
                weight: 1.0
                base_url: https://api.example.com
                api_keys:
                  - ${MISSING_1}
                  - ${MISSING_2}
        """,
    )

    router = FixedRouter()
    with pytest.raises(ValueError, match="empty"):
        register_from_models_yaml(router, yaml_path)


def test_single_api_key_form_still_works(tmp_path, monkeypatch):
    """Back-compat: existing api_key string-form is unchanged."""
    monkeypatch.setenv("ZAI_API_KEY", "single-key")

    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: glm-test
            name: glm-test
            provider: zai
            base_url: https://api.example.com
            route:
              - kind: zai
                weight: 1.0
                base_url: https://api.example.com
                api_key: ${ZAI_API_KEY}
        """,
    )

    router = FixedRouter()
    register_from_models_yaml(router, yaml_path)
    cfg = router.routes["glm-test"].adapters[0][0].config
    assert cfg.api_key == "single-key"
    assert cfg.api_keys is None


def test_missing_single_api_key_raises_in_strict_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_SINGLE", raising=False)

    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: glm-test
            name: glm-test
            provider: zai
            base_url: https://api.example.com
            route:
              - kind: zai
                weight: 1.0
                base_url: https://api.example.com
                api_key: ${MISSING_SINGLE}
        """,
    )

    router = FixedRouter()
    with pytest.raises(ValueError, match="after env expansion"):
        register_from_models_yaml(router, yaml_path)


def test_optional_route_skipped_when_key_unset(tmp_path, monkeypatch):
    """An optional route with a blank env-backed key is dropped, not the model."""
    monkeypatch.setenv("PRIMARY_KEY", "primary-key")
    monkeypatch.delenv("STAGING_API_KEY", raising=False)

    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: glm-test
            name: glm-test
            provider: zai
            base_url: https://api.example.com
            route:
              - kind: zai
                weight: 1.0
                base_url: https://api.example.com
                api_keys:
                  - ${PRIMARY_KEY}
              - kind: openai_compat
                weight: 0.01
                optional: true
                base_url: https://staging.example.com/v1
                api_keys:
                  - ${STAGING_API_KEY}
        """,
    )

    router = FixedRouter()
    register_from_models_yaml(router, yaml_path)

    # Model survives with only the primary route; the optional staging route
    # is skipped rather than dropping the whole model.
    adapters = router.routes["glm-test"].adapters
    assert len(adapters) == 1
    assert adapters[0][0].config.api_keys == ["primary-key"]


def test_optional_route_included_when_key_set(tmp_path, monkeypatch):
    """An optional route is registered normally once its key resolves."""
    monkeypatch.setenv("PRIMARY_KEY", "primary-key")
    monkeypatch.setenv("STAGING_API_KEY", "staging-key")

    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: glm-test
            name: glm-test
            provider: zai
            base_url: https://api.example.com
            route:
              - kind: zai
                weight: 1.0
                base_url: https://api.example.com
                api_keys:
                  - ${PRIMARY_KEY}
              - kind: openai_compat
                weight: 0.01
                optional: true
                base_url: https://staging.example.com/v1
                api_keys:
                  - ${STAGING_API_KEY}
        """,
    )

    router = FixedRouter()
    register_from_models_yaml(router, yaml_path)

    adapters = router.routes["glm-test"].adapters
    assert len(adapters) == 2
    assert adapters[1][0].config.api_keys == ["staging-key"]


def _clear_setting(name: str) -> None:
    """Store an empty row for *name*, as an administrator clearing it does."""
    from serving.config import app_config
    from serving.config.app_config_store import ConfigRow

    row = ConfigRow(key=name, value="", secret=True, source="admin", updated_by="admin")
    app_config._STATE.rows[name] = row
    app_config._STATE.boot_rows[name] = row
    app_config._STATE.effective[name] = row
    # reset_state() skips a process that never applied a load.
    app_config._STATE.generation += 1


_ZAI_SINGLE_KEY_YAML = """
    models:
      - id: glm-test
        name: glm-test
        provider: zai
        base_url: https://api.example.com
        route:
          - kind: zai
            weight: 1.0
            base_url: https://api.example.com
            api_key: ${ZAI_API_KEY}
    """


def _clear_numbered_zai_settings(monkeypatch) -> None:
    for index in range(1, 21):
        monkeypatch.delenv(f"ZAI_API_KEY{index}", raising=False)


def test_cleared_provider_key_setting_falls_back_to_the_providers_other_keys(tmp_path, monkeypatch):
    """A key an administrator cleared is gone, not missing: the model stays.

    Removing an env key on the Keys tab stores an empty row while the
    environment may still hold the secret. The route then serves on the
    provider's remaining key settings (and Keys-tab keys added at boot).
    """
    _clear_numbered_zai_settings(monkeypatch)
    monkeypatch.setenv("ZAI_API_KEY", "removed-but-still-in-env")
    monkeypatch.setenv("ZAI_API_KEY3", "numbered-survivor")
    _clear_setting("ZAI_API_KEY")

    router = FixedRouter()
    register_from_models_yaml(router, _write_yaml(tmp_path, _ZAI_SINGLE_KEY_YAML))

    cfg = router.routes["glm-test"].adapters[0][0].config
    assert cfg.api_key is None
    assert cfg.api_keys == ["numbered-survivor"]


@pytest.mark.parametrize(
    "shared_db_key_providers",
    [frozenset({"zai"}), None],
    ids=["provider-has-keys-tab-keys", "unknown"],
)
def test_cleared_provider_key_setting_with_no_other_setting_registers_keyless(
    monkeypatch, shared_db_key_providers
):
    """With no key setting left the route takes no static key at all.

    Keys-tab keys reach it at boot (``apply_db_keys_at_boot`` injects them into
    every adapter that is not bound to a route key). When bootstrap could not
    read which providers have them (``None``), the route is kept for that
    injection rather than dropped on a transient error.
    """
    _clear_numbered_zai_settings(monkeypatch)
    monkeypatch.setenv("ZAI_API_KEY", "removed-but-still-in-env")
    _clear_setting("ZAI_API_KEY")

    router = FixedRouter()
    register_from_models_document(
        router,
        yaml.safe_load(textwrap.dedent(_ZAI_SINGLE_KEY_YAML)),
        shared_db_key_providers=shared_db_key_providers,
    )

    adapter = router.routes["glm-test"].adapters[0][0]
    assert adapter.config.api_key is None
    assert adapter.config.api_keys is None
    assert hasattr(adapter, "add_runtime_key")


def test_cleared_setting_leaving_no_key_anywhere_skips_the_model(monkeypatch, caplog):
    """A route nothing would give a key is not registered: a dead endpoint.

    The provider has no other key setting and no Keys-tab key, which is also
    what an empty value saved on the Settings tab for its last key leads to.
    """
    _clear_numbered_zai_settings(monkeypatch)
    monkeypatch.setenv("ZAI_API_KEY", "removed-but-still-in-env")
    _clear_setting("ZAI_API_KEY")
    report = ModelLoadReport()

    router = FixedRouter()
    with caplog.at_level("WARNING"):
        register_from_models_document(
            router,
            yaml.safe_load(textwrap.dedent(_ZAI_SINGLE_KEY_YAML)),
            continue_on_missing_env=True,
            report=report,
            shared_db_key_providers=frozenset({"deepseek"}),
        )

    assert "glm-test" not in router.routes
    assert report.skipped_models == {"glm-test": []}
    assert "ZAI_API_KEY" in report.unset_env_vars
    assert "Skipping route 1 (kind=zai) of model glm-test: ZAI_API_KEY was cleared" in caplog.text

    with pytest.raises(ValueError, match="lost its key"):
        register_from_models_document(
            FixedRouter(),
            yaml.safe_load(textwrap.dedent(_ZAI_SINGLE_KEY_YAML)),
            shared_db_key_providers=frozenset(),
        )


def test_keyless_cleared_route_is_dropped_beside_a_route_that_still_serves(monkeypatch):
    """Only the dead route goes; the model keeps its other route."""
    _clear_numbered_zai_settings(monkeypatch)
    monkeypatch.setenv("ZAI_API_KEY", "removed-but-still-in-env")
    monkeypatch.setenv("FALLBACK_KEY", "fallback-live")
    _clear_setting("ZAI_API_KEY")
    document = yaml.safe_load(
        textwrap.dedent(
            """
            models:
              - id: glm-test
                name: glm-test
                provider: zai
                base_url: https://api.example.com
                route:
                  - kind: zai
                    weight: 1.0
                    base_url: https://api.example.com
                    api_key: ${ZAI_API_KEY}
                  - kind: openai_compat
                    weight: 0.5
                    base_url: https://fallback.example.com/v1
                    api_key: ${FALLBACK_KEY}
            """
        )
    )

    router = FixedRouter()
    register_from_models_document(router, document, shared_db_key_providers=frozenset())

    adapters = router.routes["glm-test"].adapters
    assert len(adapters) == 1
    assert adapters[0][0].config.api_key == "fallback-live"


@pytest.mark.parametrize(
    ("model_provider", "route"),
    [
        ("zai", {"kind": "zai"}),
        ("openai", {"kind": "openai_compat"}),
        ("zai", {"kind": "openai_compat"}),
        ("zai", {"kind": "zai", "provider": "zai-box-a"}),
        ("zai", {"kind": "openai_compat", "route_metadata": {"key_provider": "zai"}}),
        ("openrouter", {"kind": "openrouter[deepinfra]"}),
    ],
    ids=["plain", "openai-model", "cross-kind", "relabelled", "explicit-key-provider", "pinned"],
)
def test_route_key_provider_matches_the_pool_registration_uses(monkeypatch, model_provider, route):
    """The admin removal guard judges routes with the provider registration keys them by."""
    monkeypatch.setenv("ROUTE_KEY", "route-key-value")
    model = {
        "id": "parity-model",
        "name": "parity-model",
        "provider": model_provider,
        "base_url": "https://api.example.com/v1",
        "route": [
            {
                "weight": 1.0,
                "base_url": "https://api.example.com/v1",
                "api_key": "${ROUTE_KEY}",
                **route,
            }
        ],
    }

    register_from_models_document(FixedRouter(), {"models": [model]})

    registered = {
        provider for provider, adapters in dynamic_keys._adapters_by_provider.items() if adapters
    }
    assert registered == {route_key_provider(model, model["route"][0])}


def test_blank_environment_key_is_still_missing(tmp_path, monkeypatch):
    """``ZAI_API_KEY=`` in the environment, with no stored row, still skips the model."""
    _clear_numbered_zai_settings(monkeypatch)
    monkeypatch.setenv("ZAI_API_KEY", "")
    monkeypatch.setenv("ZAI_API_KEY3", "numbered-survivor")

    router = FixedRouter()
    with pytest.raises(ValueError, match="after env expansion"):
        register_from_models_yaml(router, _write_yaml(tmp_path, _ZAI_SINGLE_KEY_YAML))


def test_cleared_setting_outside_the_providers_key_settings_is_still_missing(tmp_path, monkeypatch):
    """Only a provider's own key settings have "other keys" to fall back to."""
    monkeypatch.setenv("ZAI_API_KEY", "unrelated-provider-key")
    _clear_setting("CUSTOM_GATEWAY_KEY")
    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: glm-test
            name: glm-test
            provider: zai
            base_url: https://api.example.com
            route:
              - kind: zai
                weight: 1.0
                base_url: https://api.example.com
                api_keys:
                  - ${CUSTOM_GATEWAY_KEY}
        """,
    )

    router = FixedRouter()
    with pytest.raises(ValueError, match="was cleared"):
        register_from_models_yaml(router, yaml_path)
    assert "glm-test" not in router.routes


def test_cleared_entry_beside_a_live_one_is_just_dropped(tmp_path, monkeypatch):
    """A route that still lists a configured key keeps exactly that key."""
    _clear_numbered_zai_settings(monkeypatch)
    monkeypatch.setenv("ZAI_API_KEY", "removed-but-still-in-env")
    monkeypatch.setenv("ZAI_API_KEY2", "listed-survivor")
    monkeypatch.setenv("ZAI_API_KEY3", "unlisted-numbered")
    _clear_setting("ZAI_API_KEY")
    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: glm-test
            name: glm-test
            provider: zai
            base_url: https://api.example.com
            route:
              - kind: zai
                weight: 1.0
                base_url: https://api.example.com
                api_keys:
                  - ${ZAI_API_KEY}
                  - ${ZAI_API_KEY2}
        """,
    )

    router = FixedRouter()
    register_from_models_yaml(router, yaml_path)

    assert router.routes["glm-test"].adapters[0][0].config.api_keys == ["listed-survivor"]
