"""Unit tests for OpenRouter adapter, parser, profile, and UsageInfo extension."""

from __future__ import annotations

from serving.adapters.base import ModelConfig, UsageInfo
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
