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
def test_optional_route_skipped_when_env_base_url_unset(tmp_path, monkeypatch):
    """An optional route whose env-backed base_url is unset is dropped, while the
    model's other routes stay registered — no ``<model>:unknown-api`` endpoint.

    Regression for the glm-5.1 sglang RC leg (base_url ${RC_DEPLOYMENT_URL}):
    an empty base_url used to silently register a dead provider that streamed to
    a host-less ``/v1/chat/completions`` and tripped the circuit breaker.
    """
    yaml_text = (
        "models:\n"
        "  - id: glm-test\n"
        "    name: GLM Test\n"
        "    provider: openai_compat\n"
        "    route:\n"
        "      - kind: openai_compat\n"
        "        weight: 1.0\n"
        "        base_url: https://api.example.test/v1\n"
        "      - kind: sglang\n"
        "        weight: 1.0\n"
        "        optional: true\n"
        "        base_url: ${RC_DEPLOYMENT_URL}\n"
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)
    monkeypatch.delenv("RC_DEPLOYMENT_URL", raising=False)

    exe = RouteExecutor()
    count, _infos = registry.register_from_models_yaml(exe, Path(p))

    assert count == 1
    adapters = exe.routes["glm-test"].adapters
    assert len(adapters) == 1  # optional sglang route dropped, openai_compat kept
    endpoint_ids = [adapter.config.endpoint_id for adapter, _ in adapters]
    assert all("unknown-api" not in eid for eid in endpoint_ids)


@pytest.mark.unit
def test_required_route_raises_when_env_base_url_unset(tmp_path, monkeypatch):
    """A non-optional env-backed base_url that resolves blank fails loudly
    instead of silently registering a ``<model>:unknown-api`` dead route."""
    yaml_text = (
        "models:\n"
        "  - id: oss-test\n"
        "    name: OSS Test\n"
        "    provider: vllm\n"
        "    route:\n"
        "      - kind: vllm\n"
        "        weight: 1.0\n"
        "        base_url: ${SPARK_DEPLOYMENT_URL}\n"
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)
    monkeypatch.delenv("SPARK_DEPLOYMENT_URL", raising=False)

    exe = RouteExecutor()
    with pytest.raises(registry.MissingEnvBackedKeyError):
        registry.register_from_models_yaml(exe, Path(p))


@pytest.mark.unit
def test_required_route_missing_base_url_skips_model_when_continue_on_missing_env(
    tmp_path, monkeypatch
):
    """With continue_on_missing_env=True (production bootstrap), a missing
    env-backed base_url skips the model with a warning instead of crashing boot."""
    yaml_text = (
        "models:\n"
        "  - id: oss-test\n"
        "    name: OSS Test\n"
        "    provider: vllm\n"
        "    route:\n"
        "      - kind: vllm\n"
        "        weight: 1.0\n"
        "        base_url: ${SPARK_DEPLOYMENT_URL}\n"
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)
    monkeypatch.delenv("SPARK_DEPLOYMENT_URL", raising=False)

    exe = RouteExecutor()
    count, _infos = registry.register_from_models_yaml(exe, Path(p), continue_on_missing_env=True)

    assert count == 0
    assert "oss-test" not in exe.routes


@pytest.mark.unit
def test_top_level_env_base_url_unset_skips_default_route(tmp_path, monkeypatch):
    """A top-level env-backed base_url (no explicit ``route:`` list) that
    resolves blank must not register a dead ``<model>:unknown-api`` route.

    Regression for the default/inherited-route path: ``top_cfg["base_url"]`` used
    to be expanded before the route loop, so the empty-base_url guard never saw
    the original ``${VAR}`` template for the synthesized single route.
    """
    yaml_text = (
        "models:\n"
        "  - id: top-level-test\n"
        "    name: Top Level Test\n"
        "    provider: vllm\n"
        "    base_url: ${MISSING_BASE_URL}\n"
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)
    monkeypatch.delenv("MISSING_BASE_URL", raising=False)

    # A required (non-optional) blank base_url fails loudly.
    exe = RouteExecutor()
    with pytest.raises(registry.MissingEnvBackedKeyError):
        registry.register_from_models_yaml(exe, Path(p))

    # Production bootstrap (continue_on_missing_env=True) skips the model instead.
    exe2 = RouteExecutor()
    count, _infos = registry.register_from_models_yaml(exe2, Path(p), continue_on_missing_env=True)
    assert count == 0
    assert "top-level-test" not in exe2.routes


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
def test_make_adapter_zai_uses_coding_identity_with_chat_path():
    """kind: zai routes through CodingIdentityAdapter with ZAI chat path override.

    The Z.AI GLM coding plan gates on the same coding-tool identity as the Kimi
    coding plan, so it uses CodingIdentityAdapter (a subclass of
    OpenAICompatAdapter) while preserving the zai profile and chat-path override.
    """
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
    from serving.adapters.coding_identity import CodingIdentityAdapter

    assert isinstance(adapter, CodingIdentityAdapter)
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
def test_register_kimi_coding_dynamic_keys_use_kimi_provider(tmp_path, monkeypatch):
    from serving.adapters import dynamic_keys

    dynamic_keys.reset()
    yaml_text = (
        "models:\n"
        "  - id: kimi-k2.7-code\n"
        "    name: Kimi K2.7 Code\n"
        "    provider: kimi\n"
        "    route:\n"
        "      - kind: kimi_coding\n"
        "        weight: 1.0\n"
        "        base_url: ${KIMI_CODING_BASE_URL}\n"
        "        api_keys:\n"
        "          - ${KIMI_CODING_API_KEY}\n"
        '        provider_model_id: "kimi-for-coding"\n'
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)
    monkeypatch.setenv("KIMI_CODING_BASE_URL", "https://api.kimi.com/coding/v1")
    monkeypatch.setenv("KIMI_CODING_API_KEY", "sk-coding")

    try:
        exe = RouteExecutor()
        registry.register_from_models_yaml(exe, Path(p))

        assert "kimi" in dynamic_keys.get_known_providers()
        assert "kimi_coding" not in dynamic_keys.get_known_providers()
        pools = dynamic_keys.get_pools_for_provider("kimi")
        assert len(pools) == 1
        assert pools[0].snapshot_keys() == ["sk-coding"]
    finally:
        dynamic_keys.reset()


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
def test_make_adapter_staging_uses_openai_compat_with_staging_provider():
    """kind: staging routes through OpenAICompatAdapter but keeps a 'staging' label.

    This lets a second generic OpenAI-compatible endpoint be tracked
    independently from `openai_compat` in metrics/analytics.
    """
    adapter = registry._make_adapter(
        "staging",
        {
            "id": "glm-4.7",
            "name": "GLM 4.7",
            "provider": "staging",
            "base_url": "https://api.staging-provider.com/v1",
            "api_key": "test-key",
        },
    )
    from serving.adapters.openai_compat import OpenAICompatAdapter

    assert isinstance(adapter, OpenAICompatAdapter)
    assert adapter.config.provider == "staging"


@pytest.mark.unit
def test_register_staging_and_openai_compat_get_distinct_providers(tmp_path, monkeypatch):
    """Two OpenAI-compatible upstreams get distinct provider labels via the staging kind."""
    yaml_text = (
        "models:\n"
        "  - id: glm-4.7\n"
        "    name: GLM 4.7\n"
        "    provider: openai_compat\n"
        "    route:\n"
        "      - kind: openai_compat\n"
        "        weight: 1.0\n"
        "        base_url: ${PRIMARY_BASE_URL}\n"
        "        api_key: ${PRIMARY_API_KEY}\n"
        "      - kind: staging\n"
        "        weight: 0.1\n"
        "        base_url: ${STAGING_BASE_URL}\n"
        "        api_key: ${STAGING_API_KEY}\n"
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)
    monkeypatch.setenv("PRIMARY_BASE_URL", "https://api.primary.com/v1")
    monkeypatch.setenv("PRIMARY_API_KEY", "sk-primary")
    monkeypatch.setenv("STAGING_BASE_URL", "https://api.staging.com/v1")
    monkeypatch.setenv("STAGING_API_KEY", "sk-staging")

    exe = RouteExecutor()
    registry.register_from_models_yaml(exe, Path(p))

    adapters = exe.routes["glm-4.7"].adapters
    assert len(adapters) == 2
    primary, staging = adapters[0][0], adapters[1][0]
    assert primary.config.provider == "openai_compat"
    assert staging.config.provider == "staging"
    # Distinct provider labels keep metrics/analytics cohorts separate.
    assert primary.config.provider != staging.config.provider
    # endpoint_id extraction mirrors openai_compat (hostname-derived).
    assert primary.config.endpoint_id == "glm-4.7:primary-api"
    assert staging.config.endpoint_id == "glm-4.7:staging-api"


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
    # Domain unset in this YAML: supporting the parameter is not declaring
    # which words it takes, and the registry must not invent them.
    assert adapter.config.reasoning_efforts == []


@pytest.mark.unit
def test_register_from_models_yaml_carries_reasoning_effort_domain(tmp_path, monkeypatch):
    """A declared domain reaches the adapter config the routers read it from."""
    yaml_text = (
        "models:\n"
        "  - id: glm-x\n"
        "    name: GLM X\n"
        "    provider: zai\n"
        "    supported_params: [max_tokens, stream, reasoning_effort]\n"
        "    reasoning_efforts: [low, high, max]\n"
        "    route:\n"
        "      - kind: zai\n"
        "        weight: 1.0\n"
        "        base_url: ${ZAI_URL}\n"
        "        api_key: ${ZAI_KEY}\n"
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)
    monkeypatch.setenv("ZAI_URL", "http://zai.local/v1")
    monkeypatch.setenv("ZAI_KEY", "sk-test")

    exe = RouteExecutor()
    count, _infos = registry.register_from_models_yaml(exe, Path(p))

    assert count == 1
    adapter = exe.routes["glm-x"].adapters[0][0]
    assert adapter.config.reasoning_efforts == ["low", "high", "max"]


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


@pytest.mark.unit
def test_embedding_model_multi_route_uses_fallback_adapter(tmp_path, monkeypatch):
    """An embedding model with >1 route is registered as a FallbackEmbeddingAdapter
    (primary first, staging canary as fallback), shared across aliases, and is not
    placed on the chat RouteExecutor.
    """
    from serving.servers.embedding_fallback import FallbackEmbeddingAdapter

    yaml_text = (
        "models:\n"
        "  - id: emb-model\n"
        "    name: Emb Model\n"
        "    type: embedding\n"
        "    provider: sglang\n"
        "    context_length: 8192\n"
        "    max_output_length: 0\n"
        '    aliases: ["emb-alias"]\n'
        "    route:\n"
        "      - kind: sglang\n"
        "        weight: 1.0\n"
        "        base_url: http://local.test/v1\n"
        '        provider_model_id: "BAAI/emb"\n'
        "      - kind: staging\n"
        "        optional: true\n"
        "        base_url: https://staging.test/v1\n"
        "        api_keys:\n"
        "          - ${STAGING_API_KEY}\n"
        '        provider_model_id: "emb-model"\n'
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)
    monkeypatch.setenv("STAGING_API_KEY", "sk-staging")

    exe = RouteExecutor()
    emb: dict = {}
    count, _infos = registry.register_from_models_yaml(exe, Path(p), embedding_adapters=emb)

    assert count == 2  # canonical id + alias
    assert "emb-model" in emb and "emb-alias" in emb
    wrapper = emb["emb-model"]
    assert isinstance(wrapper, FallbackEmbeddingAdapter)
    # Catalog/metadata surfaces the primary route; staging is the fallback.
    assert wrapper.config.provider == "sglang"
    # The alias shares the same wrapper instance.
    assert emb["emb-alias"] is wrapper
    # Embedding models never land on the weighted chat executor.
    assert "emb-model" not in exe.routes


@pytest.mark.unit
def test_embedding_model_single_route_uses_plain_adapter(tmp_path, monkeypatch):
    """When the optional staging route is skipped (key unset), the embedding
    model is left with a single route and keeps using its plain adapter — no
    fallback wrapper, preserving prior behavior.
    """
    from serving.servers.embedding_fallback import FallbackEmbeddingAdapter

    yaml_text = (
        "models:\n"
        "  - id: emb-solo\n"
        "    name: Emb Solo\n"
        "    type: embedding\n"
        "    provider: sglang\n"
        "    context_length: 8192\n"
        "    max_output_length: 0\n"
        "    route:\n"
        "      - kind: sglang\n"
        "        weight: 1.0\n"
        "        base_url: http://local.test/v1\n"
        '        provider_model_id: "BAAI/emb"\n'
        "      - kind: staging\n"
        "        optional: true\n"
        "        base_url: https://staging.test/v1\n"
        "        api_keys:\n"
        "          - ${STAGING_API_KEY}\n"
        '        provider_model_id: "emb-solo"\n'
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)
    # Unset STAGING_API_KEY -> the optional staging route is dropped at load.
    monkeypatch.delenv("STAGING_API_KEY", raising=False)

    exe = RouteExecutor()
    emb: dict = {}
    registry.register_from_models_yaml(exe, Path(p), embedding_adapters=emb)

    adapter = emb["emb-solo"]
    assert not isinstance(adapter, FallbackEmbeddingAdapter)
    assert adapter.config.provider == "sglang"


@pytest.mark.unit
def test_two_models_claiming_one_alias_is_reported_without_changing_routing(
    tmp_path, monkeypatch, caplog
):
    """An ambiguous alias is reported, and routing is left exactly as it was.

    `register_route` writes aliases into the route table unconditionally, so
    the second model to claim a name already wins today. Refusing to start
    would turn a deployment that has been serving that way into one that will
    not boot — from a change whose only purpose is to hand the cloud agent a
    translation table.

    So the load warns and carries on, and the *shipped* configuration is what
    CI holds to a stricter standard. Fail-closed at startup is a later,
    separate decision, once the live configs are known clean.
    """
    yaml_text = (
        "models:\n"
        "  - id: model-a\n"
        "    name: Model A\n"
        "    provider: zai\n"
        "    base_url: ${ZAI_BASE_URL}\n"
        "    api_key: ${LLAMA_API_KEY}\n"
        '    aliases: ["shared-name"]\n'
        "  - id: model-b\n"
        "    name: Model B\n"
        "    provider: zai\n"
        "    base_url: ${ZAI_BASE_URL}\n"
        "    api_key: ${LLAMA_API_KEY}\n"
        '    aliases: ["shared-name"]\n'
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)
    monkeypatch.setenv("ZAI_BASE_URL", "http://zai.local")
    monkeypatch.setenv("LLAMA_API_KEY", "sk-test")

    exe = RouteExecutor()
    with caplog.at_level("WARNING"):
        registry.register_from_models_yaml(exe, Path(p))

    assert any("shared-name" in record.getMessage() for record in caplog.records)
    # Unchanged behaviour: last one loaded still wins, exactly as before.
    assert exe.routes["shared-name"].canonical_model_id == "model-b"


@pytest.mark.unit
def test_one_model_repeating_its_own_alias_is_fine(tmp_path, monkeypatch):
    """Duplication within a model is a typo, not an ambiguity — it still
    resolves to exactly one place, so refusing to start would be a stricter
    rule than the problem calls for."""
    yaml_text = (
        "models:\n"
        "  - id: model-a\n"
        "    name: Model A\n"
        "    provider: zai\n"
        "    base_url: ${ZAI_BASE_URL}\n"
        "    api_key: ${LLAMA_API_KEY}\n"
        '    aliases: ["same", "same"]\n'
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)
    monkeypatch.setenv("ZAI_BASE_URL", "http://zai.local")
    monkeypatch.setenv("LLAMA_API_KEY", "sk-test")

    exe = RouteExecutor()
    registry.register_from_models_yaml(exe, Path(p))

    assert exe.routes["same"].canonical_model_id == "model-a"


@pytest.mark.unit
def test_shipped_config_has_no_ambiguous_alias():
    """CI holds the configuration to the standard the loader only warns about.

    The loader warns rather than refuses so that a deployment already serving
    an ambiguous alias keeps working. That is the right call for *running*
    code and the wrong one for what we ship: an alias whose meaning depends on
    YAML order is a request that means different things after an unrelated
    reordering.

    Checked against the file itself rather than a loaded registry, because the
    loader has already collapsed the duplicate by the time it returns.
    """
    import yaml

    for path in sorted(
        Path(__file__).resolve().parents[2].glob("distributions/*/config/models.yaml")
    ):
        document = yaml.safe_load(path.read_text()) or {}
        owner: dict[str, str] = {}
        clashes: list[str] = []
        for model in document.get("models") or []:
            model_id = str(model.get("id"))
            for alias in model.get("aliases") or []:
                previous = owner.get(str(alias))
                if previous is not None and previous != model_id:
                    clashes.append(f"{alias!r}: {previous!r} and {model_id!r}")
                owner[str(alias)] = model_id
        assert not clashes, f"{path.name} has aliases claimed by two models: {clashes}"

        # And no alias may be spelled like some model's canonical id, which
        # would shadow that model for anything resolving by name.
        canonical_ids = {str(model.get("id")) for model in document.get("models") or []}
        shadowed = sorted(set(owner) & canonical_ids)
        assert not shadowed, f"{path.name} has aliases shadowing real model ids: {shadowed}"


@pytest.mark.unit
def test_an_existing_alias_still_reaches_the_same_adapter(tmp_path, monkeypatch):
    """**The regression this whole change must not cause.**

    The internal catalog is additive: it tells the cloud agent which canonical
    id an alias means. Nothing about how an ordinary request is routed may
    move — same route, same canonical id, same adapter object as the canonical
    id resolves to.

    Asserted on identity, not equality: the alias and the canonical share one
    `RouteConfig` by reference, and a copy would be a behaviour change that
    compares equal.
    """
    yaml_text = (
        "models:\n"
        "  - id: real-model\n"
        "    name: Real Model\n"
        "    provider: zai\n"
        "    base_url: ${ZAI_BASE_URL}\n"
        "    api_key: ${LLAMA_API_KEY}\n"
        '    aliases: ["legacy-name"]\n'
    )
    p = tmp_path / "models.yaml"
    p.write_text(yaml_text)
    monkeypatch.setenv("ZAI_BASE_URL", "http://zai.local")
    monkeypatch.setenv("LLAMA_API_KEY", "sk-test")

    exe = RouteExecutor()
    registry.register_from_models_yaml(exe, Path(p))

    assert exe.routes["legacy-name"] is exe.routes["real-model"]
    assert exe.canonical_id("legacy-name") == "real-model"
    assert exe.routes["legacy-name"].adapters[0][0] is exe.routes["real-model"].adapters[0][0]
