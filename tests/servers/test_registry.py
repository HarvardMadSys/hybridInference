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
        "    provider: zai\n"
        "    base_url: ${ZAI_BASE_URL}\n"
        "    api_key: ${LLAMA_API_KEY}\n"
        "    context_length: 8192\n"
        "    max_output_length: 1024\n"
        '    aliases: ["alias-1"]\n'
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)

    monkeypatch.setenv("ZAI_BASE_URL", "http://zai.local")
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
    assert adapter.config.base_url == "http://zai.local"
    assert adapter.config.api_key == "sk-test"


@pytest.mark.unit
def test_register_from_models_yaml_merges_route_extra_body(tmp_path):
    yaml_text = (
        "models:\n"
        "  - id: qwen-test\n"
        "    name: Qwen Test\n"
        "    provider: sglang\n"
        "    extra_body:\n"
        "      shared: model-default\n"
        "    route:\n"
        "      - kind: sglang\n"
        "        weight: 1.0\n"
        "        base_url: http://sglang.local/v1\n"
        "        extra_body:\n"
        "          shared: route-override\n"
        "          chat_template_kwargs:\n"
        "            enable_thinking: false\n"
    )
    path = tmp_path / "models.yaml"
    path.write_text(yaml_text)

    executor = RouteExecutor()
    registry.register_from_models_yaml(executor, path)

    adapter = executor.routes["qwen-test"].adapters[0][0]
    assert adapter.config.extra_body == {
        "shared": "route-override",
        "chat_template_kwargs": {"enable_thinking": False},
    }


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
def test_make_adapter_zai_uses_openai_compat_with_chat_path():
    """kind: zai routes through OpenAICompatAdapter with ZAI chat path override."""
    adapter = registry._make_adapter(
        "zai",
        {
            "id": "glm-5",
            "name": "GLM-5",
            "provider": "zai",
            "base_url": "https://api.z.ai/api/coding/paas/v4/",
            "api_key": "test-key",
        },
    )
    from serving.adapters.openai_compat import OpenAICompatAdapter

    assert isinstance(adapter, OpenAICompatAdapter)
    assert adapter.config.provider_profile == "zai"
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
def test_make_adapter_kimi_uses_openai_compat_with_profile():
    """kind: kimi routes through OpenAICompatAdapter with the Kimi profile.

    The Kimi Code coding-plan base_url ends in /v1, so the adapter posts to
    .../coding/v1/chat/completions (no chat_path override needed).
    """
    adapter = registry._make_adapter(
        "kimi",
        {
            "id": "kimi-k2.7-code",
            "name": "Kimi K2.7 Code",
            "provider": "kimi",
            "base_url": "https://api.kimi.com/coding/v1",
            "api_key": "test-key",
            "provider_model_id": "kimi-for-coding",
        },
    )
    from serving.adapters.openai_compat import OpenAICompatAdapter

    assert isinstance(adapter, OpenAICompatAdapter)
    assert adapter.config.provider_profile == "kimi"
    assert adapter.config.provider_model_id == "kimi-for-coding"


@pytest.mark.unit
def test_kimi_profile_does_not_support_guided_json():
    """Kimi (Moonshot) speaks OpenAI response_format, not vLLM guided_json."""
    from serving.adapters.profiles import ProviderProfile, supports_guided_json

    assert supports_guided_json(ProviderProfile.KIMI) is False


@pytest.mark.unit
def test_register_kimi_coding_and_metered_routes_get_distinct_endpoint_ids(tmp_path, monkeypatch):
    """The two Kimi upstreams must get distinct endpoint IDs for independent tracking."""
    yaml_text = (
        "models:\n"
        "  - id: kimi-k2.7-code\n"
        "    name: Kimi K2.7 Code\n"
        "    provider: kimi\n"
        "    route:\n"
        "      - kind: kimi\n"
        "        weight: 1.0\n"
        "        base_url: ${KIMI_CODING_BASE_URL}\n"
        "        api_keys:\n"
        "          - ${KIMI_CODING_API_KEY}\n"
        '        provider_model_id: "kimi-for-coding"\n'
        "      - kind: kimi\n"
        "        weight: 0.1\n"
        "        base_url: ${MOONSHOT_BASE_URL}\n"
        "        api_keys:\n"
        "          - ${MOONSHOT_API_KEY}\n"
        '        provider_model_id: "kimi-latest"\n'
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)
    monkeypatch.setenv("KIMI_CODING_BASE_URL", "https://api.kimi.com/coding/v1")
    monkeypatch.setenv("KIMI_CODING_API_KEY", "sk-coding")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://api.moonshot.ai/v1")
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-moonshot")

    exe = RouteExecutor()
    registry.register_from_models_yaml(exe, Path(p))

    adapters = exe.routes["kimi-k2.7-code"].adapters
    assert len(adapters) == 2
    coding, metered = adapters[0][0], adapters[1][0]
    assert coding.config.base_url == "https://api.kimi.com/coding/v1"
    assert coding.config.provider_model_id == "kimi-for-coding"
    # Distinct endpoint IDs keep circuit-breaker / availability tracking separate.
    assert coding.config.endpoint_id == "kimi-k2.7-code:kimi-api"
    assert metered.config.endpoint_id == "kimi-k2.7-code:moonshot-api"
    assert coding.config.endpoint_id != metered.config.endpoint_id


@pytest.mark.unit
def test_make_adapter_cliproxy_uses_openai_compat():
    """kind: cliproxy routes through OpenAICompatAdapter with a cliproxy provider label."""
    adapter = registry._make_adapter(
        "cliproxy",
        {
            "id": "gpt-5.5",
            "name": "GPT-5.5",
            "provider": "cliproxy",
            "base_url": "http://cliproxy.local/v1",
            "api_key": "test-key",
            "provider_model_id": "gpt-5.5",
        },
    )
    from serving.adapters.openai_compat import OpenAICompatAdapter

    assert isinstance(adapter, OpenAICompatAdapter)
    assert adapter.config.provider == "cliproxy"
    assert adapter.config.provider_model_id == "gpt-5.5"


@pytest.mark.unit
def test_register_from_models_yaml_cliproxy_gpt55(tmp_path, monkeypatch):
    yaml_text = (
        "models:\n"
        "  - id: gpt-5.5\n"
        "    name: GPT-5.5\n"
        "    provider: cliproxy\n"
        "    required_role: internal\n"
        "    provider_model_id: gpt-5.5\n"
        "    context_length: 1050000\n"
        "    max_output_length: 128000\n"
        "    supports_tools: true\n"
        "    supports_structured_output: true\n"
        "    supported_params: [max_tokens, stream, tools, tool_choice, reasoning_effort]\n"
        "    input_modalities: [text, image]\n"
        "    output_modalities: [text]\n"
        "    route:\n"
        "      - kind: cliproxy\n"
        "        weight: 1.0\n"
        "        base_url: ${CLI_PROXY_BASE_URL}\n"
        "        api_key: ${CLI_PROXY_API_KEY}\n"
        "        provider_model_id: gpt-5.5\n"
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)
    monkeypatch.setenv("CLI_PROXY_BASE_URL", "http://cliproxy.local/v1")
    monkeypatch.setenv("CLI_PROXY_API_KEY", "sk-test")

    exe = RouteExecutor()
    count, _infos = registry.register_from_models_yaml(exe, Path(p))

    assert count == 1
    route = exe.routes["gpt-5.5"]
    assert route.required_role == "internal"
    adapter = route.adapters[0][0]
    assert adapter.config.provider == "cliproxy"
    assert adapter.config.base_url == "http://cliproxy.local/v1"
    assert adapter.config.api_key == "sk-test"
    assert adapter.config.context_length == 1050000
    assert adapter.config.max_output_length == 128000
    assert "reasoning_effort" in adapter.config.supported_params


@pytest.mark.unit
def test_register_from_models_yaml_keeps_model_provider_when_route_kind_differs(
    tmp_path, monkeypatch
):
    yaml_text = (
        "models:\n"
        "  - id: gpt-5.5\n"
        "    name: GPT-5.5\n"
        "    provider: openai\n"
        "    required_role: internal\n"
        "    provider_model_id: gpt-5.5\n"
        "    context_length: 1050000\n"
        "    max_output_length: 128000\n"
        "    supports_tools: true\n"
        "    supports_structured_output: true\n"
        "    supported_params: [max_tokens, stream, tools, tool_choice, reasoning_effort]\n"
        "    input_modalities: [text, image]\n"
        "    output_modalities: [text]\n"
        "    route:\n"
        "      - kind: openai_compat\n"
        "        weight: 1.0\n"
        "        base_url: ${CLI_PROXY_BASE_URL}\n"
        "        api_key: ${CLI_PROXY_API_KEY}\n"
        "        provider_model_id: gpt-5.5\n"
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)
    monkeypatch.setenv("CLI_PROXY_BASE_URL", "http://cliproxy.local/v1")
    monkeypatch.setenv("CLI_PROXY_API_KEY", "sk-test")

    exe = RouteExecutor()
    count, infos = registry.register_from_models_yaml(exe, Path(p))

    assert count == 1
    route = exe.routes["gpt-5.5"]
    adapter = route.adapters[0][0]
    assert adapter.config.provider == "openai"
    assert adapter.config.base_url == "http://cliproxy.local/v1"
    assert adapter.config.provider_model_id == "gpt-5.5"
    assert [info.model_id for info in infos] == ["gpt-5.5"]


@pytest.mark.unit
def test_register_from_models_yaml_skips_bad_model_and_continues(tmp_path, monkeypatch):
    yaml_text = (
        "models:\n"
        "  - id: qwen3.6-35b\n"
        "    name: Qwen3.6 35B\n"
        "    provider: sglang\n"
        "    route:\n"
        "      - kind: sglang\n"
        "        weight: 1.0\n"
        "        base_url: http://host.docker.internal:8001\n"
        "        api_keys:\n"
        "          - ${SGLANG_API_KEY}\n"
        "  - id: gpt-5.5\n"
        "    name: GPT-5.5\n"
        "    provider: cliproxy\n"
        "    required_role: internal\n"
        "    route:\n"
        "      - kind: cliproxy\n"
        "        weight: 1.0\n"
        "        base_url: ${CLI_PROXY_BASE_URL}\n"
        "        api_key: ${CLI_PROXY_API_KEY}\n"
        "        provider_model_id: gpt-5.5\n"
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)
    monkeypatch.delenv("SGLANG_API_KEY", raising=False)
    monkeypatch.setenv("CLI_PROXY_BASE_URL", "http://cliproxy.local/v1")
    monkeypatch.setenv("CLI_PROXY_API_KEY", "sk-test")

    exe = RouteExecutor()
    count, infos = registry.register_from_models_yaml(exe, Path(p), continue_on_missing_env=True)

    assert count == 1
    assert "qwen3.6-35b" not in exe.routes
    assert "gpt-5.5" in exe.routes
    assert exe.routes["gpt-5.5"].required_role == "internal"
    assert [info.model_id for info in infos] == ["gpt-5.5"]


@pytest.mark.unit
def test_register_from_models_yaml_skips_missing_single_api_key_in_bootstrap_mode(
    tmp_path, monkeypatch, caplog
):
    yaml_text = (
        "models:\n"
        "  - id: qwen3.6-35b\n"
        "    name: Qwen3.6 35B\n"
        "    provider: sglang\n"
        "    route:\n"
        "      - kind: sglang\n"
        "        weight: 1.0\n"
        "        base_url: http://host.docker.internal:8001\n"
        "        api_key: ${SGLANG_API_KEY}\n"
        "  - id: gpt-5.5\n"
        "    name: GPT-5.5\n"
        "    provider: cliproxy\n"
        "    required_role: internal\n"
        "    route:\n"
        "      - kind: cliproxy\n"
        "        weight: 1.0\n"
        "        base_url: ${CLI_PROXY_BASE_URL}\n"
        "        api_key: ${CLI_PROXY_API_KEY}\n"
        "        provider_model_id: gpt-5.5\n"
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)
    monkeypatch.delenv("SGLANG_API_KEY", raising=False)
    monkeypatch.setenv("CLI_PROXY_BASE_URL", "http://cliproxy.local/v1")
    monkeypatch.setenv("CLI_PROXY_API_KEY", "sk-test")

    exe = RouteExecutor()
    with caplog.at_level("WARNING"):
        count, infos = registry.register_from_models_yaml(
            exe, Path(p), continue_on_missing_env=True
        )

    assert count == 1
    assert "qwen3.6-35b" not in exe.routes
    assert "gpt-5.5" in exe.routes
    assert "Skipping model 'qwen3.6-35b'" in caplog.text
    assert "Traceback" not in caplog.text
    assert [info.model_id for info in infos] == ["gpt-5.5"]


@pytest.mark.unit
def test_register_from_models_yaml_does_not_register_dynamic_keys_for_skipped_model(
    tmp_path, monkeypatch
):
    from serving.adapters import dynamic_keys

    yaml_text = (
        "models:\n"
        "  - id: mixed-model\n"
        "    name: Mixed Model\n"
        "    provider: zai\n"
        "    route:\n"
        "      - kind: zai\n"
        "        weight: 1.0\n"
        "        base_url: https://api.example.com\n"
        "        api_keys:\n"
        "          - ${LIVE_KEY}\n"
        "      - kind: zai\n"
        "        weight: 0.5\n"
        "        base_url: https://api.example.com\n"
        "        api_keys:\n"
        "          - ${MISSING_KEY}\n"
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)
    monkeypatch.setenv("LIVE_KEY", "live-key")
    monkeypatch.delenv("MISSING_KEY", raising=False)

    exe = RouteExecutor()
    registry.register_from_models_yaml(exe, Path(p), continue_on_missing_env=True)

    assert "mixed-model" not in exe.routes
    assert "zai" not in dynamic_keys.get_known_providers()
    assert dynamic_keys.get_pools_for_provider("zai") == []


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
      budget_alpha: 0.5
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
    assert by_id["model-with-router"].router_params == {"budget_alpha": 0.5}
    assert by_id["model-without-router"].router is None
    assert by_id["model-without-router"].router_params is None


@pytest.mark.unit
def test_register_from_models_yaml_propagates_routewise_route_metadata(tmp_path):
    yaml = """
models:
  - id: routewise-model
    name: RouteWise Model
    provider: openai_compat
    base_url: http://example.com/v1
    router: routewise
    route:
      - kind: openai_compat
        weight: 1.0
        base_url: http://example.com/v1
        provider_type: quota
        routewise_pool: glm-paid-pool
        quota_pool: chutes-glm-daily
        quota_source:
          provider: chutes
          usage_label: Daily requests
          unit: requests
        quota:
          limit: 5000
          window: daily
      - kind: openai_compat
        weight: 1.0
        base_url: http://example-two.com/v1
        provider_type: concurrency
        concurrency_pool: featherless-glm
        concurrency:
          limit: 4
"""
    p = tmp_path / "models.yaml"
    p.write_text(yaml)
    exe = RouteExecutor()
    registry.register_from_models_yaml(exe, Path(p))

    first = exe.routes["routewise-model"].adapters[0][0].config
    second = exe.routes["routewise-model"].adapters[1][0].config

    assert first.provider_type == "quota"
    assert first.routewise_pool == "glm-paid-pool"
    assert first.quota_pool == "chutes-glm-daily"
    assert first.quota_source == {
        "provider": "chutes",
        "usage_label": "Daily requests",
        "unit": "requests",
    }
    assert first.quota == {"limit": 5000, "window": "daily"}

    assert second.provider_type == "concurrency"
    assert second.concurrency_pool == "featherless-glm"
    assert second.concurrency == {"limit": 4}
