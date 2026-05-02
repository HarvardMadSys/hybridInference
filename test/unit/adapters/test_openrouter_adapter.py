"""Unit tests for OpenRouter adapter, parser, profile, and UsageInfo extension."""

from __future__ import annotations

from typing import Any

from serving.adapters.base import ModelConfig, UsageInfo
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.adapters.profiles import (
    ProviderProfile,
    get_usage_normalizer,
    normalize_usage_openrouter,
)


def test_usage_info_default_upstream_cost_is_none() -> None:
    info = UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    assert info.upstream_cost_usd is None


def test_usage_info_to_dict_omits_upstream_cost() -> None:
    info = UsageInfo(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        upstream_cost_usd=0.00342,
    )
    d = info.to_dict()
    assert "upstream_cost_usd" not in d
    assert d == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def test_model_config_default_openrouter_pinned_provider_is_none() -> None:
    cfg = ModelConfig(id="m", name="M", provider="openrouter", base_url="https://x")
    assert cfg.openrouter_pinned_provider is None


def test_model_config_accepts_openrouter_pinned_provider() -> None:
    cfg = ModelConfig(
        id="m",
        name="M",
        provider="openrouter",
        base_url="https://x",
        openrouter_pinned_provider="deepinfra",
    )
    assert cfg.openrouter_pinned_provider == "deepinfra"


def test_provider_profile_has_openrouter() -> None:
    assert ProviderProfile("openrouter") is ProviderProfile.OPENROUTER


def test_normalize_usage_openrouter_with_cost_and_cached_tokens() -> None:
    info = normalize_usage_openrouter(
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cost": 0.00342,
            "prompt_tokens_details": {"cached_tokens": 30},
        }
    )
    assert info.prompt_tokens == 100
    assert info.completion_tokens == 50
    assert info.total_tokens == 150
    assert info.cache_read_tokens == 30
    assert info.upstream_cost_usd == 0.00342


def test_normalize_usage_openrouter_without_cost() -> None:
    info = normalize_usage_openrouter(
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    )
    assert info.upstream_cost_usd is None
    assert info.cache_read_tokens == 0


def test_normalize_usage_openrouter_handles_flat_cache_field() -> None:
    """When OpenRouter (or its upstream) returns cache_read_tokens flat, use it."""
    info = normalize_usage_openrouter(
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cache_read_tokens": 25,
        }
    )
    assert info.cache_read_tokens == 25


def test_get_usage_normalizer_returns_openrouter_normalizer() -> None:
    normalizer = get_usage_normalizer(ProviderProfile.OPENROUTER)
    info = normalizer({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.5})
    assert info.upstream_cost_usd == 0.5


def test_normalize_usage_openrouter_string_cost_is_parsed() -> None:
    """A string-encoded numeric cost (rare but possible from proxies) is parsed."""
    info = normalize_usage_openrouter(
        {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "cost": "0.00342",
        }
    )
    assert info.upstream_cost_usd == 0.00342


def test_normalize_usage_openrouter_non_numeric_cost_is_dropped(caplog) -> None:
    """A non-numeric cost (e.g. dict, garbage string) is dropped with a warning."""
    import logging

    with caplog.at_level(logging.WARNING):
        info = normalize_usage_openrouter(
            {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "cost": "not-a-number",
            }
        )
    assert info.upstream_cost_usd is None
    assert any("non-numeric cost" in rec.message for rec in caplog.records)


def test_normalize_usage_openrouter_negative_cost_is_dropped(caplog) -> None:
    """A negative cost is dropped with a warning (defensive against bad upstream data)."""
    import logging

    with caplog.at_level(logging.WARNING):
        info = normalize_usage_openrouter(
            {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "cost": -1.5,
            }
        )
    assert info.upstream_cost_usd is None
    assert any("negative cost" in rec.message for rec in caplog.records)


def _make_compat_cfg(**overrides: Any) -> ModelConfig:
    base: dict[str, Any] = {
        "id": "dummy-model",
        "name": "Dummy",
        "provider": "openai_compat",
        "base_url": "https://example.test/v1",
        "api_key": "sk-test",
        "provider_model_id": "dummy-upstream",
        "supports_tools": False,
        "supports_structured_output": False,
        "supported_params": ["temperature", "top_p", "max_tokens"],
    }
    base.update(overrides)
    return ModelConfig(**base)


def test_augment_payload_default_is_noop() -> None:
    cfg = _make_compat_cfg()
    adapter = OpenAICompatAdapter(cfg)
    payload = {"model": "x", "messages": []}
    out = adapter._augment_payload(dict(payload), stream=False)
    assert out == payload
