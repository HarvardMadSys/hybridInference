"""RouteWise provider-candidate extraction.

This module is the boundary between FreeInference route configuration and the
RouteWise body router.  It reads route-level metadata carried on
``ModelConfig`` and normalizes it into stable provider candidates before any
effective-cost or LP logic runs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from serving.adapters.base import BaseAdapter


class SubscriptionType(Enum):
    """RouteWise subscription tier for one adapter endpoint."""

    QUOTA = "quota"
    CONCURRENCY = "concurrency"
    API = "api"


@dataclass(frozen=True, slots=True)
class CandidatePricing:
    """Parsed provider pricing in USD per million tokens."""

    prompt: float = 0.0
    completion: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0

    @classmethod
    def from_raw(
        cls,
        raw: Mapping[str, Any] | None,
        *,
        context: str = "pricing",
    ) -> CandidatePricing:
        raw = raw or {}
        if not isinstance(raw, Mapping):
            raise ValueError(f"{context} must be a mapping; got {type(raw).__name__}")
        return cls(
            prompt=_parse_float(raw.get("prompt", 0.0), f"{context}.prompt"),
            completion=_parse_float(raw.get("completion", 0.0), f"{context}.completion"),
            cache_read=_parse_float(
                raw.get("input_cache_reads", 0.0),
                f"{context}.input_cache_reads",
            ),
            cache_write=_parse_float(
                raw.get("input_cache_writes", 0.0),
                f"{context}.input_cache_writes",
            ),
        )


@dataclass(frozen=True, slots=True)
class QuotaSource:
    """Provider-side quota signal used to recover S_Q state."""

    provider: str
    usage_label: str
    unit: str

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any], *, context: str = "quota_source") -> QuotaSource:
        provider = _required_str(raw, "provider", context)
        usage_label = _required_str(raw, "usage_label", context)
        unit = _required_str(raw, "unit", context)
        return cls(provider=provider, usage_label=usage_label, unit=unit)


@dataclass(frozen=True, slots=True)
class ProviderCandidate:
    """RouteWise provider metadata before route-time feasibility/cost checks.

    The candidate is the unit RouteWise optimizes over. If the underlying
    adapter has a multi-key pool, that pool is treated as one aggregate
    endpoint: ``endpoint_id``, latency/error profile, and quota_source must all
    describe the pool-level resource. Key-level scarcity requires separate
    route entries instead of hidden adapter-level key rotation.
    """

    endpoint_id: str
    model_id: str
    adapter: BaseAdapter
    subscription_type: SubscriptionType
    weight: float
    pricing: CandidatePricing
    routewise_pool: str
    quota_pool: str | None = None
    concurrency_pool: str | None = None
    quota_source: QuotaSource | None = None
    quota_config: dict[str, Any] = field(default_factory=dict)
    concurrency_config: dict[str, Any] = field(default_factory=dict)

    @property
    def tier(self) -> str:
        return self.subscription_type.value


def build_provider_candidates(
    model_id: str,
    adapters_with_weights: Sequence[tuple[BaseAdapter, float]],
) -> list[ProviderCandidate]:
    """Normalize a model route into RouteWise provider candidates."""

    candidates: list[ProviderCandidate] = []
    seen_endpoint_ids: set[str] = set()

    for adapter, raw_weight in adapters_with_weights:
        weight = float(raw_weight)
        if weight <= 0:
            continue

        endpoint_base = endpoint_id_for_adapter(adapter)
        if endpoint_base in seen_endpoint_ids:
            raise ValueError(
                f"RouteWise endpoint_id {endpoint_base!r} is configured more than once "
                f"for model {model_id!r}; endpoint_id must be unique so latency profiles "
                "and request logs share one stable key."
            )
        seen_endpoint_ids.add(endpoint_base)
        endpoint_id = endpoint_base

        sub_type = subscription_type_for_adapter(adapter)
        config = adapter.config
        pricing = CandidatePricing.from_raw(
            _mapping_attr(config, "pricing") or {},
            context=f"{endpoint_id}.pricing",
        )
        routewise_pool = _optional_str_attr(config, "routewise_pool") or model_id

        quota_pool: str | None = None
        concurrency_pool: str | None = None
        quota_source: QuotaSource | None = None
        quota_config: dict[str, Any] = {}
        concurrency_config: dict[str, Any] = {}

        if sub_type is SubscriptionType.QUOTA:
            quota_pool = _optional_str_attr(config, "quota_pool") or f"{model_id}:{endpoint_id}"
            quota_source_raw = _mapping_attr(config, "quota_source")
            quota_source = (
                QuotaSource.from_raw(quota_source_raw, context=f"{endpoint_id}.quota_source")
                if quota_source_raw is not None
                else None
            )
            quota_config = dict(_mapping_attr(config, "quota") or {})

        if sub_type is SubscriptionType.CONCURRENCY:
            concurrency_pool = (
                _optional_str_attr(config, "concurrency_pool") or f"{model_id}:{endpoint_id}"
            )
            concurrency_config = dict(_mapping_attr(config, "concurrency") or {})

        candidates.append(
            ProviderCandidate(
                endpoint_id=endpoint_id,
                model_id=model_id,
                adapter=adapter,
                subscription_type=sub_type,
                weight=weight,
                pricing=pricing,
                routewise_pool=routewise_pool,
                quota_pool=quota_pool,
                concurrency_pool=concurrency_pool,
                quota_source=quota_source,
                quota_config=quota_config,
                concurrency_config=concurrency_config,
            )
        )

    return candidates


def endpoint_id_for_adapter(adapter: BaseAdapter) -> str:
    """Return the configured endpoint id, falling back to provider."""

    config = adapter.config
    endpoint_id = _optional_str_attr(config, "endpoint_id")
    if endpoint_id:
        return endpoint_id
    provider = _optional_str_attr(config, "provider")
    return provider or "unknown"


def subscription_type_for_adapter(adapter: BaseAdapter) -> SubscriptionType:
    """Parse adapter ``subscription_type``, defaulting unknown values to API."""

    raw = _optional_str_attr(adapter.config, "subscription_type") or SubscriptionType.API.value
    try:
        return SubscriptionType(raw)
    except ValueError:
        return SubscriptionType.API


def _parse_float(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric; got {value!r}") from exc


def _required_str(raw: Mapping[str, Any], key: str, context: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _optional_str_attr(config: Any, name: str) -> str | None:
    value = _safe_attr(config, name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _mapping_attr(config: Any, name: str) -> Mapping[str, Any] | None:
    value = _safe_attr(config, name)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping; got {type(value).__name__}")
    return value


def _safe_attr(config: Any, name: str) -> Any | None:
    value = getattr(config, name, None)
    # Unit tests often use MagicMock configs; missing attrs on a MagicMock look
    # like more MagicMocks rather than None. Treat those as absent route fields.
    if type(value).__module__ == "unittest.mock":
        return None
    return value
