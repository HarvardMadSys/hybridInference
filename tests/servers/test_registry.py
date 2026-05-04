from __future__ import annotations

from pathlib import Path

import pytest

from routing.executor import RouteExecutor
from serving.servers import registry


@pytest.mark.unit
def test_register_from_models_yaml_env_expansion_and_aliases(tmp_path, monkeypatch):
    # Prepare YAML with env placeholders and alias
    yaml_text = (
        "models:\n"
        "  - id: test-model\n"
        "    name: Test Model\n"
        "    provider: zhipu\n"
        "    base_url: ${ZHIPU_BASE_URL}\n"
        "    api_key: ${LLAMA_API_KEY}\n"
        "    context_length: 8192\n"
        "    max_output_length: 1024\n"
        '    aliases: ["alias-1"]\n'
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)

    monkeypatch.setenv("ZHIPU_BASE_URL", "http://zhipu.local")
    monkeypatch.setenv("LLAMA_API_KEY", "sk-test")

    exe = RouteExecutor()
    count, _infos = registry.register_from_models_yaml(exe, Path(p))
    # Should register canonical id + alias
    assert count == 2
    assert "test-model" in exe.routes and "alias-1" in exe.routes

    # Adapter config should reflect env-expanded fields
    adapters = exe.routes["test-model"].adapters
    assert adapters
    adapter = adapters[0][0]
    assert adapter.config.base_url == "http://zhipu.local"
    assert adapter.config.api_key == "sk-test"


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

    exe = RouteExecutor()
    count, _infos = registry.register_from_models_yaml(exe, Path(p))
    assert count == 1
    assert "vllm-model" in exe.routes
    adapters = exe.routes["vllm-model"].adapters
    assert len(adapters) == 1
    assert adapters[0][0].config.provider == "vllm"


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


@pytest.mark.unit
def test_make_adapter_deepseek_uses_openai_compat_with_profile():
    """kind: deepseek routes through OpenAICompatAdapter with DeepSeek profile."""
    adapter = registry._make_adapter(
        "deepseek",
        {
            "id": "deepseek-chat",
            "name": "DeepSeek Chat",
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "test-key",
        },
    )
    from serving.adapters.openai_compat import OpenAICompatAdapter

    assert isinstance(adapter, OpenAICompatAdapter)
    assert adapter.config.provider_profile == "deepseek"


@pytest.mark.unit
def test_make_adapter_zhipu_uses_openai_compat_with_chat_path():
    """kind: zhipu routes through OpenAICompatAdapter with Zhipu chat path override."""
    adapter = registry._make_adapter(
        "zhipu",
        {
            "id": "glm-5",
            "name": "GLM-5",
            "provider": "zhipu",
            "base_url": "https://api.z.ai/api/coding/paas/v4/",
            "api_key": "test-key",
        },
    )
    from serving.adapters.openai_compat import OpenAICompatAdapter

    assert isinstance(adapter, OpenAICompatAdapter)
    assert adapter.config.provider_profile == "zhipu"
    assert adapter.config.chat_path == "/chat/completions"


@pytest.mark.unit
def test_make_adapter_minimax_uses_openai_compat_with_profile():
    """kind: minimax routes through OpenAICompatAdapter with MiniMax profile."""
    adapter = registry._make_adapter(
        "minimax",
        {
            "id": "minimax-m2.7",
            "name": "MiniMax M2.7",
            "provider": "minimax",
            "base_url": "https://api.minimax.io/v1",
            "api_key": "test-key",
        },
    )
    from serving.adapters.openai_compat import OpenAICompatAdapter

    assert isinstance(adapter, OpenAICompatAdapter)
    assert adapter.config.provider_profile == "minimax"


@pytest.mark.unit
def test_register_from_models_yaml_invalid_processor_override_raises(tmp_path):
    yaml_text = (
        "models:\n"
        "  - id: glm-local\n"
        "    name: GLM Local\n"
        "    provider: openai_compat\n"
        "    base_url: http://localhost:8000/v1\n"
        "    route:\n"
        "      - kind: openai_compat\n"
        "        weight: 1.0\n"
        "        base_url: http://localhost:8000/v1\n"
        '        provider_model_id: "glm-4.7-flash"\n'
        '        processor: "not_a_processor"\n'
    )
    p = tmp_path / "invalid_processor.yaml"
    p.write_text(yaml_text)

    exe = RouteExecutor()
    with pytest.raises(ValueError, match="Unknown processor override 'not_a_processor'"):
        registry.register_from_models_yaml(exe, Path(p))


@pytest.mark.unit
def test_register_from_models_yaml_propagates_router_fields(tmp_path):
    """`router` and `router_params` from models.yaml flow into ModelRegistrationInfo."""
    yaml = """
models:
  - id: model-with-router
    name: M1
    provider: openai_compat
    base_url: http://example.com/v1
    router: routewise
    router_params:
      daily_quota: 1000
    route:
      - kind: openai_compat
        weight: 1.0
        base_url: http://example.com/v1
  - id: model-without-router
    name: M2
    provider: openai_compat
    base_url: http://example.com/v1
    route:
      - kind: openai_compat
        weight: 1.0
        base_url: http://example.com/v1
"""
    p = tmp_path / "models.yaml"
    p.write_text(yaml)
    exe = RouteExecutor()
    _count, infos = registry.register_from_models_yaml(exe, Path(p))

    by_id = {i.model_id: i for i in infos}
    assert by_id["model-with-router"].router == "routewise"
    assert by_id["model-with-router"].router_params == {"daily_quota": 1000}
    assert by_id["model-without-router"].router is None
    assert by_id["model-without-router"].router_params is None
