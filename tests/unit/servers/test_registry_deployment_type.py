"""A deployment declaration belongs to an endpoint, not a model or protocol."""

from __future__ import annotations

import pytest
import yaml

from routing.executor import RouteExecutor
from serving.adapters.base import ModelConfig
from serving.servers.registry import register_from_models_yaml


def _model() -> dict:
    return {
        "id": "engine-model",
        "name": "Engine model",
        "provider": "deepseek",
        "route": [
            {
                "kind": "openai_compat",
                "base_url": "http://10.0.0.5:8000/v1",
                "deployment_type": "self_hosted",
                "provider_type": "concurrency",
                "concurrency_pool": "shared-engine",
            },
            {"kind": "deepseek", "base_url": "https://api.example.com/v1"},
        ],
    }


def _load(tmp_path, model: dict, **kwargs) -> list:
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump({"models": [model]}))
    router = RouteExecutor()
    register_from_models_yaml(router, path, **kwargs)
    return [adapter.config for adapter, _ in router.routes[model["id"]].adapters]


def test_deployment_is_route_only_and_keeps_resource_type_and_endpoint_identity(tmp_path):
    model = _model()
    owned, vendor = _load(tmp_path, model)
    assert owned.deployment_type == "self_hosted"
    assert owned.provider_type == "concurrency"
    assert owned.concurrency_pool == "shared-engine"
    assert vendor.deployment_type == "auto"

    del model["route"][0]["deployment_type"]
    auto, _ = _load(tmp_path, model)
    assert auto.deployment_type == "auto"
    assert auto.endpoint_id == owned.endpoint_id
    assert auto.provider == owned.provider
    assert auto.base_url == owned.base_url


def test_localhost_provider_override_loads(tmp_path):
    model = _model()
    model["route"][0].update(base_url="http://localhost:8000/v1", deployment_type="provider_api")
    local_proxy, _ = _load(tmp_path, model)
    assert local_proxy.deployment_type == "provider_api"


@pytest.mark.parametrize("shorthand", [False, True])
def test_model_level_declaration_is_rejected_even_without_credentials(tmp_path, shorthand):
    model = _model()
    model["deployment_type"] = "self_hosted"
    if shorthand:
        del model["route"]
        model.update(base_url="http://10.0.0.5:8000/v1", api_key="${UNSET_ENGINE_KEY}")
    else:
        model["route"][0]["api_key"] = "${UNSET_ENGINE_KEY}"
    with pytest.raises(ValueError, match=r"deployment_type.*must be declared on a route"):
        _load(tmp_path, model, continue_on_missing_env=True)


@pytest.mark.parametrize("value", [None, True, 1, "local", "SELF_HOSTED", " self_hosted ", [], {}])
def test_invalid_route_declaration_is_rejected_before_optional_credential_skip(tmp_path, value):
    model = _model()
    model["route"][0].update(deployment_type=value, optional=True, api_key="${UNSET_ENGINE_KEY}")
    with pytest.raises(ValueError, match=r"deployment_type must be one of.*route 1"):
        _load(tmp_path, model, continue_on_missing_env=True)


@pytest.mark.parametrize("value", [None, True, 1, "local", "SELF_HOSTED", [], {}])
def test_direct_model_config_also_rejects_invalid_deployment(value):
    with pytest.raises(ValueError, match="deployment_type must be one of"):
        ModelConfig(
            id="engine-model",
            name="Engine model",
            provider="openai_compat",
            base_url="http://10.0.0.5:8000/v1",
            deployment_type=value,
        )
