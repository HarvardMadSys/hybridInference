"""Tests for RouteWise provider-candidate extraction."""

from __future__ import annotations

import pytest

from routing.routewise.candidates import (
    ProviderCandidate,
    QuotaSource,
    SubscriptionType,
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
    subscription_type: str = "api",
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
            subscription_type=subscription_type,
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
        subscription_type="quota",
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
        subscription_type="concurrency",
        routewise_pool="glm-paid-pool",
        concurrency_pool="featherless-glm",
        concurrency={"limit": 4},
    )
    api = _adapter(
        provider="zai",
        endpoint_id="glm-test:zai-api",
        subscription_type="api",
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
    assert quota_candidate.subscription_type is SubscriptionType.QUOTA
    assert quota_candidate.routewise_pool == "glm-paid-pool"
    assert quota_candidate.quota_pool == "chutes-glm-daily"
    assert quota_candidate.quota_source == QuotaSource(
        provider="chutes",
        usage_label="Daily requests",
        unit="requests",
    )
    assert quota_candidate.quota_config == {"limit": 5000, "window": "daily"}

    concurrency_candidate = by_id["glm-test:featherless-api"]
    assert concurrency_candidate.subscription_type is SubscriptionType.CONCURRENCY
    assert concurrency_candidate.concurrency_pool == "featherless-glm"
    assert concurrency_candidate.concurrency_config == {"limit": 4}

    api_candidate = by_id["glm-test:zai-api"]
    assert api_candidate.subscription_type is SubscriptionType.API
    assert api_candidate.pricing.prompt == pytest.approx(1.2)
    assert api_candidate.pricing.completion == pytest.approx(4.0)


@pytest.mark.unit
def test_build_provider_candidates_uses_stable_defaults_and_skips_zero_weight():
    quota = _adapter(provider="chutes", endpoint_id="quota-shared", subscription_type="quota")
    api = _adapter(provider="zai", endpoint_id="api-shared", subscription_type="api")
    zero = _adapter(provider="ignored", endpoint_id="ignored", subscription_type="api")

    candidates = build_provider_candidates("glm-test", [(quota, 1.0), (api, 1.0), (zero, 0.0)])

    assert [c.endpoint_id for c in candidates] == ["quota-shared", "api-shared"]
    assert candidates[0].routewise_pool == "glm-test"
    assert candidates[0].quota_pool == "glm-test:quota-shared"
    assert candidates[1].routewise_pool == "glm-test"
    assert candidates[1].quota_pool is None


@pytest.mark.unit
def test_build_provider_candidates_rejects_duplicate_endpoint_id():
    quota = _adapter(provider="chutes", endpoint_id="shared", subscription_type="quota")
    api = _adapter(provider="zai", endpoint_id="shared", subscription_type="api")

    with pytest.raises(ValueError, match=r"endpoint_id 'shared'.*more than once"):
        build_provider_candidates("glm-test", [(quota, 1.0), (api, 1.0)])


@pytest.mark.unit
def test_build_provider_candidates_rejects_malformed_quota_source():
    quota = _adapter(
        provider="chutes",
        subscription_type="quota",
        quota_source={"provider": "chutes", "unit": "requests"},
    )

    with pytest.raises(ValueError, match=r"quota_source\.usage_label"):
        build_provider_candidates("glm-test", [(quota, 1.0)])


@pytest.mark.unit
def test_unknown_subscription_type_defaults_to_api_candidate():
    adapter = _adapter(subscription_type="not-a-tier")

    candidates = build_provider_candidates("glm-test", [(adapter, 1.0)])

    assert candidates[0].subscription_type is SubscriptionType.API
