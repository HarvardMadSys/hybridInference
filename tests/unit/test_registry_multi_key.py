"""Loader tests for `api_keys` multi-key route entries."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

from routing.executor import RouteExecutor
from serving.servers.registry import register_from_models_yaml

if TYPE_CHECKING:
    from pathlib import Path


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(textwrap.dedent(body))
    return path


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

    router = RouteExecutor()
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

    router = RouteExecutor()
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

    router = RouteExecutor()
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

    router = RouteExecutor()
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

    router = RouteExecutor()
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

    router = RouteExecutor()
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

    router = RouteExecutor()
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

    router = RouteExecutor()
    register_from_models_yaml(router, yaml_path)

    adapters = router.routes["glm-test"].adapters
    assert len(adapters) == 2
    assert adapters[1][0].config.api_keys == ["staging-key"]
