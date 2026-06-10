"""Tests for RouteWise provider-candidate extraction."""

from __future__ import annotations

import pytest

from routing.routewise.candidates import (
    ProviderCandidate,
    ProviderType,
    QuotaSource,
    build_provider_candidates,
)
from serving.adapters.base import ModelConfig


class _Adapter:
    def __init__(self, config: ModelConfig) -> None:
        self.config = config


def _adapter(
    *,
    model_id: str = "glm-test",
    provider: str = "zai",
    endpoint_id: str | None = None,
    provider_type: str = "on_demand",
    routewise_pool: str | None = None,
    quota_pool: str | None = None,
    concurrency_pool: str | None = None,
    quota_source: dict[str, str] | None = None,
    quota: dict[str, object] | None = None,
    concurrency: dict[str, object] | None = None,
    prompt: str = "1.2",
    completion: str = "4.0",
) -> _Adapter:
    return _Adapter(
        ModelConfig(
            id=model_id,
            name=model_id,
            provider=provider,
            base_url=f"https://{provider}.example/v1",
            endpoint_id=endpoint_id,
            provider_type=provider_type,
            routewise_pool=routewise_pool,
            quota_pool=quota_pool,
            concurrency_pool=concurrency_pool,
            quota_source=quota_source,
            quota=quota,
            concurrency=concurrency,
            pricing={"prompt": prompt, "completion": completion},
        )
    )


@pytest.mark.unit
def test_build_provider_candidates_preserves_routewise_metadata():
    quota = _adapter(
        provider="chutes",
        endpoint_id="glm-test:chutes-api",
        provider_type="quota",
        routewise_pool="glm-paid-pool",
        quota_pool="chutes-glm-daily",
        quota_source={
            "provider": "chutes",
            "usage_label": "Daily requests",
            "unit": "requests",
        },
        quota={"limit": 5000, "window": "daily"},
    )
    concurrency = _adapter(
        provider="featherless",
        endpoint_id="glm-test:featherless-api",
        provider_type="concurrency",
        routewise_pool="glm-paid-pool",
        concurrency_pool="featherless-glm",
        concurrency={"limit": 4},
    )
    api = _adapter(
        provider="zai",
        endpoint_id="glm-test:zai-api",
        provider_type="on_demand",
        routewise_pool="glm-paid-pool",
    )

    candidates = build_provider_candidates(
        "glm-test",
        [(quota, 1.0), (concurrency, 2.0), (api, 3.0)],
    )

    assert [c.endpoint_id for c in candidates] == [
        "glm-test:chutes-api",
        "glm-test:featherless-api",
        "glm-test:zai-api",
    ]

    by_id: dict[str, ProviderCandidate] = {c.endpoint_id: c for c in candidates}
    quota_candidate = by_id["glm-test:chutes-api"]
    assert quota_candidate.provider_type is ProviderType.QUOTA
    assert quota_candidate.routewise_pool == "glm-paid-pool"
    assert quota_candidate.quota_pool == "chutes-glm-daily"
    assert quota_candidate.quota_source == QuotaSource(
        provider="chutes",
        usage_label="Daily requests",
        unit="requests",
    )
    assert quota_candidate.quota_policy is not None
    assert quota_candidate.quota_policy.limit == 5000
    assert quota_candidate.quota_policy.window.type == "daily"

    concurrency_candidate = by_id["glm-test:featherless-api"]
    assert concurrency_candidate.provider_type is ProviderType.CONCURRENCY
    assert concurrency_candidate.concurrency_pool == "featherless-glm"
    assert concurrency_candidate.concurrency_policy is not None
    assert concurrency_candidate.concurrency_policy.limit == 4

    api_candidate = by_id["glm-test:zai-api"]
    assert api_candidate.provider_type is ProviderType.ON_DEMAND
    assert api_candidate.pricing.prompt == pytest.approx(1.2)
    assert api_candidate.pricing.completion == pytest.approx(4.0)


@pytest.mark.unit
def test_build_provider_candidates_uses_stable_defaults_and_skips_zero_weight():
    quota = _adapter(
        provider="chutes",
        endpoint_id="quota-shared",
        provider_type="quota",
        quota={"limit": 100},
    )
    api = _adapter(provider="zai", endpoint_id="api-shared", provider_type="on_demand")
    zero = _adapter(provider="ignored", endpoint_id="ignored", provider_type="on_demand")

    candidates = build_provider_candidates("glm-test", [(quota, 1.0), (api, 1.0), (zero, 0.0)])

    assert [c.endpoint_id for c in candidates] == ["quota-shared", "api-shared"]
    assert candidates[0].routewise_pool == "glm-test"
    assert candidates[0].quota_pool == "glm-test:quota-shared"
    assert candidates[1].routewise_pool == "glm-test"
    assert candidates[1].quota_pool is None


@pytest.mark.unit
def test_build_provider_candidates_rejects_duplicate_endpoint_id():
    quota = _adapter(
        provider="chutes",
        endpoint_id="shared",
        provider_type="quota",
        quota={"limit": 100},
    )
    api = _adapter(provider="zai", endpoint_id="shared", provider_type="on_demand")

    with pytest.raises(ValueError, match=r"endpoint_id 'shared'.*more than once"):
        build_provider_candidates("glm-test", [(quota, 1.0), (api, 1.0)])


@pytest.mark.unit
def test_quota_route_requires_quota_block():
    quota = _adapter(provider="chutes", provider_type="quota")

    with pytest.raises(ValueError, match=r"requires a route-level 'quota:' block"):
        build_provider_candidates("glm-test", [(quota, 1.0)])


@pytest.mark.unit
def test_concurrency_route_requires_concurrency_block():
    concurrency = _adapter(provider="featherless", provider_type="concurrency")

    with pytest.raises(ValueError, match=r"requires a route-level 'concurrency:' block"):
        build_provider_candidates("glm-test", [(concurrency, 1.0)])


@pytest.mark.unit
def test_build_provider_candidates_rejects_malformed_quota_source():
    quota = _adapter(
        provider="chutes",
        provider_type="quota",
        quota_source={"provider": "chutes", "unit": "requests"},
    )

    with pytest.raises(ValueError, match=r"quota_source\.usage_label"):
        build_provider_candidates("glm-test", [(quota, 1.0)])


@pytest.mark.unit
def test_unknown_provider_type_raises():
    adapter = _adapter(provider_type="not-a-provider-type")

    with pytest.raises(ValueError, match="provider_type must be one of"):
        build_provider_candidates("glm-test", [(adapter, 1.0)])
