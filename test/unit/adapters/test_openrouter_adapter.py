"""Unit tests for OpenRouter adapter, parser, profile, and UsageInfo extension."""

from __future__ import annotations

from serving.adapters.base import UsageInfo


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
