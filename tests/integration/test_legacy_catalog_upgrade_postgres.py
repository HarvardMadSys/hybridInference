"""An upgraded deployment serves the model catalog it served before the upgrade.

Revision 0012 copies the stored YAML route catalog into the ``MODEL_CATALOG``
setting as it is, and lifts its inline credentials into secret settings. The
catalog below has the shapes the production catalog had before the move to
Admin (``ce389d385^:config/models.yaml``): aliases two models claim, an
embedding model whose output length is 0, a route without a kind, a top-level
anchor block, RouteWise resource routes and an inline credential. When the
setting's check refused any of them, the stored catalog was set aside and the
upgraded gateway served no model at all.
"""

from __future__ import annotations

import logging

import pytest

from routing.routers import FixedRouter
from serving.config import app_config
from serving.config.app_config_store import AppConfigStore
from serving.servers import bootstrap
from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

_INLINE_KEY = "sk-legacy-inline-0001"

LEGACY_CATALOG = f"""\
# Values like ${{ENV_VAR}} will be expanded from environment variables.

# The staging canary every model falls back to, written once.
x-staging: &staging
  kind: staging
  weight: 0.01
  optional: true
  base_url: https://staging.example.org/v1
  api_keys:
    - ${{STAGING_API_KEY}}

models:
  - id: glm-5.1
    name: GLM-5.1
    provider: zai
    quantization: "fp8"
    context_length: 200000
    max_output_length: 128000
    supports_tools: true
    supported_params: [temperature, top_p, max_tokens, stop, stream, tool_stream, thinking]
    aliases: ["freeinference-glm-5.1"]
    pricing:
      prompt: "2.8"
      completion: "6.4"
      input_cache_reads: "0.8"
    route:
      - kind: zai
        weight: 1.0
        base_url: ${{ZAI_BASE_URL}}
        api_keys:
          - ${{ZAI_API_KEY}}
        provider_model_id: "glm-5.1"
      # Ollama Cloud fallback
      - kind: ollama
        weight: 0.01
        base_url: ${{OLLAMA_BASE_URL}}
        api_keys:
          - ${{OLLAMA_API_KEY}}
        provider_model_id: "glm-5.1"
      - *staging

  - id: minimax-fast
    name: MiniMax Fast
    provider: minimax
    context_length: 204800
    max_output_length: 131072
    aliases: ["MiniMax-Fast"]
    router: routewise
    router_params:
      budget_alpha: 0.5
      latency_hedge_mode: probability_target
      envelope_bootstrap_donor_models: ["glm-5.1"]
    pricing:
      prompt: "0.3"
      completion: "1.2"
    route:
      - kind: chutes
        weight: 1.0
        base_url: ${{CHUTES_BASE_URL}}
        api_keys:
          - ${{CHUTES_API_KEY}}
        provider_model_id: "MiniMaxAI/MiniMax-M2.5-TEE"
        provider_type: quota
        quota_pool: chutes-minimax-fast-daily
        quota_source:
          provider: chutes
          usage_label: "Daily requests"
          unit: requests
        quota:
          limit: 5000
      - kind: openrouter[deepinfra]
        weight: 1.0
        base_url: https://openrouter.ai/api/v1
        api_keys:
          - ${{OPENROUTER_API_KEY}}
        provider_type: on_demand
        provider_model_id: "minimax/minimax-m2.5"

  - id: kimi-k2.7-code
    name: Kimi K2.7 Code
    provider: kimi
    input_modalities: ["text", "image", "video"]
    aliases: ["Kimi-K2.7-Code", "kimi-k2.7"]
    route:
      - kind: kimi
        weight: 1.0
        base_url: ${{KIMI_BASE_URL}}
        api_keys:
          - ${{KIMI_API_KEY}}
        provider_model_id: "kimi-for-coding"

  # The same two aliases again: the model that loads last answers them.
  - id: kimi-k3
    name: Kimi K3
    provider: kimi
    aliases: ["Kimi-K2.7-Code", "kimi-k2.7"]
    route:
      - kind: kimi
        weight: 1.0
        base_url: ${{KIMI_BASE_URL}}
        api_keys:
          - ${{KIMI_API_KEY}}
        provider_model_id: "kimi-k3"
      - *staging

  - id: qwen3.6-35b
    name: Qwen3.6 35B
    provider: sglang
    aliases: ["qwen3.6:35b", "claude-sonnet-5"]
    route:
      - kind: sglang
        weight: 1.0
        base_url: ${{LOCAL_DEPLOYMENT_URL}}
        api_keys:
          - ${{LOCAL_API_KEY}}
        extra_body:
          chat_template_kwargs:
            enable_thinking: false

  - id: deepseek-v4-flash
    name: DeepSeek V4 Flash
    provider: deepseek
    required_role: pro
    context_length: 1000000
    max_output_length: 393216
    aliases: ["deepseek-v4", "claude-sonnet-5"]
    route:
      # optional: skipped while H200_DEPLOYMENT_URL is unset.
      - kind: sglang
        weight: 1.0
        optional: true
        base_url: ${{H200_DEPLOYMENT_URL}}
        api_keys:
          - ${{LOCAL_API_KEY}}
      # No kind: the model's provider, deepseek.
      - weight: 1.0
        base_url: https://api.deepseek.com
        api_key: {_INLINE_KEY}
        provider_model_id: "deepseek-v4-flash"

  # ===== Embedding Models =====
  - id: bge-m3
    name: BGE-M3
    type: embedding
    provider: sglang
    input_modalities: ["text"]
    output_modalities: ["embedding"]
    context_length: 8192
    max_output_length: 0
    pricing:
      prompt: "0"
      completion: "0"
    route:
      - kind: sglang
        weight: 1.0
        base_url: ${{LOCAL_DEPLOYMENT_URL}}
        provider_model_id: "BAAI/bge-m3"
        api_keys:
          - ${{LOCAL_API_KEY}}
      - kind: staging
        optional: true
        base_url: https://staging.example.org/v1
        api_keys:
          - ${{STAGING_API_KEY}}
"""

_SETTINGS = {
    "ZAI_BASE_URL": "https://api.z.example/v4",
    "ZAI_API_KEY": "zai-key",
    "OLLAMA_BASE_URL": "https://ollama.example/v1",
    "OLLAMA_API_KEY": "ollama-key",
    "CHUTES_BASE_URL": "https://llm.chutes.example/v1",
    "CHUTES_API_KEY": "chutes-key",
    "OPENROUTER_API_KEY": "openrouter-key",
    "KIMI_BASE_URL": "https://api.kimi.example/v1",
    "KIMI_API_KEY": "kimi-key",
    "LOCAL_DEPLOYMENT_URL": "http://10.0.0.5:8000/v1",
    "LOCAL_API_KEY": "local-key",
}


async def test_the_production_catalog_shape_registers_after_the_upgrade(monkeypatch, caplog):
    for name, value in _SETTINGS.items():
        monkeypatch.setenv(name, value)
    for name in ("STAGING_API_KEY", "H200_DEPLOYMENT_URL", "MODEL_CATALOG"):
        monkeypatch.delenv(name, raising=False)
    caplog.set_level(logging.WARNING)

    async with mig.scratch_database() as database:
        await mig.upgrade(database, "0011_trim_oversized_preferences")
        async with mig.connected(database) as conn:
            await conn.execute(
                "INSERT INTO route_catalog(kind,body,source) VALUES ('models',$1,'yaml_import')",
                LEGACY_CATALOG,
            )
        await mig.upgrade(database)
        async with mig.connected(database) as conn:
            await app_config.load(AppConfigStore(conn), bootstrap=False)
            # The copied catalog is the one in force, not set aside as invalid.
            assert app_config.validation_problem("MODEL_CATALOG") is None
            router = FixedRouter()
            embedding, infos = await bootstrap._init_router_and_models(router)

    assert router.routes
    assert {info.model_id for info in infos} == {
        "glm-5.1",
        "minimax-fast",
        "kimi-k2.7-code",
        "kimi-k3",
        "qwen3.6-35b",
        "deepseek-v4-flash",
        "bge-m3",
    }
    assert {"glm-5.1", "freeinference-glm-5.1", "minimax-fast", "deepseek-v4"} <= set(router.routes)
    # A name two models claim answers for the one that loads last, as before.
    assert router.routes["kimi-k2.7"].canonical_model_id == "kimi-k3"
    assert router.routes["claude-sonnet-5"].canonical_model_id == "deepseek-v4-flash"
    assert "'claude-sonnet-5' is claimed by both" in caplog.text
    # The embedding model whose output length is 0.
    assert "bge-m3" in embedding
    # The route without a kind is a deepseek route, and keeps the credential
    # the upgrade moved into a secret setting.
    ((adapter, _weight),) = router.routes["deepseek-v4-flash"].adapters
    assert adapter.config.provider == "deepseek"
    assert adapter.config.api_key == _INLINE_KEY
    assert {info.model_id: info.router for info in infos}["minimax-fast"] == "routewise"
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
