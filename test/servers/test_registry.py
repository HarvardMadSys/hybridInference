from __future__ import annotations

from pathlib import Path

import pytest

from routing.routers import FixedRouter
from serving.servers import registry


@pytest.mark.unit
def test_register_from_models_yaml_env_expansion_and_aliases(tmp_path, monkeypatch):
    # Prepare YAML with env placeholders and alias
    yaml_text = (
        "models:\n"
        "  - id: test-model\n"
        "    name: Test Model\n"
        "    provider: llama\n"
        "    base_url: ${LLAMA_BASE_URL}\n"
        "    api_key: ${LLAMA_API_KEY}\n"
        "    context_length: 8192\n"
        "    max_output_length: 1024\n"
        '    aliases: ["alias-1"]\n'
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)

    monkeypatch.setenv("LLAMA_BASE_URL", "http://llama.local")
    monkeypatch.setenv("LLAMA_API_KEY", "sk-test")

    exe = FixedRouter()
    count, model_infos = registry.register_from_models_yaml(exe, Path(p))
    # Should register canonical id + alias
    assert count == 2
    assert "test-model" in exe.routes and "alias-1" in exe.routes

    # Adapter config should reflect env-expanded fields
    adapters = exe.routes["test-model"].adapters
    assert adapters
    adapter = adapters[0][0]
    assert adapter.config.base_url == "http://llama.local"
    assert adapter.config.api_key == "sk-test"

    # ModelRegistrationInfo should be populated
    assert len(model_infos) == 1
    assert model_infos[0].model_id == "test-model"
    assert model_infos[0].aliases == ["alias-1"]
    assert model_infos[0].strategy is None


@pytest.mark.unit
def test_register_defaults_to_single_route_when_no_route_list(tmp_path, monkeypatch):
    yaml_text = (
        "models:\n"
        "  - id: vllm-model\n"
        "    name: VLLM Model\n"
        "    provider: vllm\n"
        "    base_url: http://vllm.local\n"
    )
    p = tmp_path / "models2.yaml"
    p.write_text(yaml_text)

    exe = FixedRouter()
    count, model_infos = registry.register_from_models_yaml(exe, Path(p))
    assert count == 1
    assert "vllm-model" in exe.routes
    adapters = exe.routes["vllm-model"].adapters
    assert len(adapters) == 1
    assert adapters[0][0].config.provider == "vllm"

    assert len(model_infos) == 1
    assert model_infos[0].model_id == "vllm-model"
    assert model_infos[0].strategy is None


@pytest.mark.unit
def test_routing_strategy_parsed_from_yaml(tmp_path, monkeypatch):
    """Per-model routing_strategy field is correctly parsed from YAML."""
    yaml_text = (
        "models:\n"
        "  - id: model-nimbus\n"
        "    name: Nimbus Model\n"
        "    provider: vllm\n"
        "    base_url: http://vllm.local\n"
        "    routing_strategy: nimbus\n"
        "  - id: model-routewise\n"
        "    name: RouteWise Model\n"
        "    provider: vllm\n"
        "    base_url: http://vllm.local\n"
        "    routing_strategy: routewise\n"
        "  - id: model-default\n"
        "    name: Default Model\n"
        "    provider: vllm\n"
        "    base_url: http://vllm.local\n"
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)

    exe = FixedRouter()
    count, model_infos = registry.register_from_models_yaml(exe, Path(p))
    assert count == 3
    assert len(model_infos) == 3

    by_id = {info.model_id: info for info in model_infos}
    assert by_id["model-nimbus"].strategy == "nimbus"
    assert by_id["model-routewise"].strategy == "routewise"
    assert by_id["model-default"].strategy is None


@pytest.mark.unit
def test_subscription_type_parsed_from_yaml(tmp_path, monkeypatch):
    """subscription_type flows from route entries to ModelConfig and ModelRegistrationInfo."""
    yaml_text = (
        "models:\n"
        "  - id: hybrid-model\n"
        "    name: Hybrid Model\n"
        "    provider: vllm\n"
        "    base_url: http://local.host\n"
        "    route:\n"
        "      - kind: vllm\n"
        "        weight: 1.0\n"
        "        base_url: http://local.host\n"
        "        subscription_type: quota\n"
        "      - kind: openai_compat\n"
        "        weight: 1.0\n"
        "        base_url: http://remote.api\n"
        "        subscription_type: api\n"
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)

    exe = FixedRouter()
    count, model_infos = registry.register_from_models_yaml(exe, Path(p))
    assert count == 1

    # Verify subscription_type on adapters
    adapters = exe.routes["hybrid-model"].adapters
    assert len(adapters) == 2
    assert adapters[0][0].config.subscription_type == "quota"
    assert adapters[1][0].config.subscription_type == "api"

    # Verify route_subscription_types on ModelRegistrationInfo
    assert len(model_infos) == 1
    assert model_infos[0].route_subscription_types == ["quota", "api"]


@pytest.mark.unit
def test_subscription_type_defaults_to_api(tmp_path, monkeypatch):
    """Omitting subscription_type from route defaults to 'api'."""
    yaml_text = (
        "models:\n"
        "  - id: simple-model\n"
        "    name: Simple Model\n"
        "    provider: vllm\n"
        "    base_url: http://local.host\n"
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)

    exe = FixedRouter()
    count, model_infos = registry.register_from_models_yaml(exe, Path(p))
    assert count == 1
    adapters = exe.routes["simple-model"].adapters
    assert adapters[0][0].config.subscription_type == "api"
    assert model_infos[0].route_subscription_types == ["api"]


@pytest.mark.unit
def test_make_adapter_unknown_kind_raises():
    with pytest.raises(ValueError):
        registry._make_adapter(
            "unknown",
            {  # type: ignore[arg-type]
                "id": "m",
                "name": "M",
                "provider": "unknown",
                "base_url": "http://x",
            },
        )
