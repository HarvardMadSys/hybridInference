"""RouteWise provider-candidate extraction.

This module is the boundary between FreeInference route configuration and the
RouteWise body router.  It reads route-level metadata carried on
``ModelConfig`` and normalizes it into stable provider candidates before any
effective-cost or LP logic runs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from serving.adapters.base import BaseAdapter


class ProviderType(Enum):
    """RouteWise provider category for one adapter endpoint."""

    ON_DEMAND = "on_demand"
    QUOTA = "quota"
    CONCURRENCY = "concurrency"


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
        """Parse provider pricing metadata from route configuration."""
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
        """Parse a provider quota-source descriptor."""
        provider = _required_str(raw, "provider", context)
        usage_label = _required_str(raw, "usage_label", context)
        unit = _required_str(raw, "unit", context)
        return cls(provider=provider, usage_label=usage_label, unit=unit)


_DURATION_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def _parse_duration_sec(value: Any, context: str) -> float:
    """Parse a duration like ``"5h"``, ``"30m"``, or plain seconds."""
    if isinstance(value, bool):
        raise ValueError(f"{context} must be a duration like '5h' or seconds; got {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        text = value.strip().lower()
        unit = _DURATION_UNITS.get(text[-1:]) if text else None
        try:
            seconds = float(text[:-1]) * unit if unit is not None else float(text)
        except ValueError as exc:
            raise ValueError(
                f"{context} must be a duration like '5h', '30m', or seconds; got {value!r}"
            ) from exc
    else:
        raise ValueError(f"{context} must be a duration like '5h' or seconds; got {value!r}")
    if seconds <= 0:
        raise ValueError(f"{context} must be positive; got {value!r}")
    return seconds


def _parse_positive_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{context} must be a positive integer; got {value!r}")
    if value < 1:
        raise ValueError(f"{context} must be >= 1; got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class QuotaWindow:
    """Reset window for one provider quota pool.

    ``daily`` resets at midnight in ``timezone``; ``rolling`` is a sliding
    window of ``duration_sec`` seconds (e.g. a Claude-style 5-hour quota).
    """

    type: str
    timezone: str = "UTC"
    duration_sec: float = 0.0

    @classmethod
    def from_raw(cls, raw: Any, *, context: str) -> QuotaWindow:
        """Parse a window descriptor; a bare string is sugar for ``{type: ...}``."""
        if raw is None:
            return cls(type="daily")
        if isinstance(raw, str):
            raw = {"type": raw}
        if not isinstance(raw, Mapping):
            raise ValueError(f"{context} must be a string or mapping; got {type(raw).__name__}")
        window_type = str(raw.get("type", "")).strip().lower()
        if window_type == "daily":
            unknown = set(raw) - {"type", "timezone"}
            if unknown:
                raise ValueError(f"{context}: unknown keys {sorted(unknown)} for type 'daily'")
            timezone = str(raw.get("timezone", "UTC")).strip() or "UTC"
            try:
                ZoneInfo(timezone)
            except Exception as exc:
                raise ValueError(
                    f"{context}.timezone {timezone!r} is not a known IANA timezone"
                ) from exc
            return cls(type="daily", timezone=timezone)
        if window_type == "rolling":
            unknown = set(raw) - {"type", "duration"}
            if unknown:
                raise ValueError(f"{context}: unknown keys {sorted(unknown)} for type 'rolling'")
            if "duration" not in raw:
                raise ValueError(f"{context}: type 'rolling' requires a duration (e.g. '5h')")
            return cls(
                type="rolling",
                duration_sec=_parse_duration_sec(raw["duration"], f"{context}.duration"),
            )
        raise ValueError(f"{context}.type must be 'daily' or 'rolling'; got {raw.get('type')!r}")


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    """Route-level quota resource rule for one provider.

    ``limit`` is always required: it bounds the local optimistic counter and
    cross-checks the provider-reported limit for snapshot-backed pools.
    """

    limit: int
    window: QuotaWindow

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any], *, context: str) -> QuotaPolicy:
        """Parse a route-level ``quota:`` block."""
        if not isinstance(raw, Mapping):
            raise ValueError(f"{context} must be a mapping with at least 'limit'")
        unknown = set(raw) - {"limit", "window"}
        if unknown:
            raise ValueError(f"{context}: unknown keys {sorted(unknown)}")
        return cls(
            limit=_parse_positive_int(raw.get("limit"), f"{context}.limit"),
            window=QuotaWindow.from_raw(raw.get("window"), context=f"{context}.window"),
        )


@dataclass(frozen=True, slots=True)
class ConcurrencyPolicy:
    """Route-level concurrency slot rule for one provider."""

    limit: int

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any], *, context: str) -> ConcurrencyPolicy:
        """Parse a route-level ``concurrency:`` block."""
        if not isinstance(raw, Mapping):
            raise ValueError(f"{context} must be a mapping with at least 'limit'")
        unknown = set(raw) - {"limit"}
        if unknown:
            raise ValueError(f"{context}: unknown keys {sorted(unknown)}")
        return cls(limit=_parse_positive_int(raw.get("limit"), f"{context}.limit"))


@dataclass(frozen=True, slots=True)
class ProviderCandidate:
    """RouteWise provider metadata before route-time feasibility/cost checks.

    The candidate is the unit RouteWise optimizes over. If the underlying
    adapter has a multi-key pool, that pool is treated as one aggregate
    endpoint: ``endpoint_id``, latency/error profile, and quota_source must all
    describe the pool-level resource. Key-level scarcity requires separate
    route entries instead of hidden adapter-level key rotation.

    ``routewise_pool`` groups a model's candidates into one routing/envelope
    domain (default: the model id). ``quota_pool`` / ``concurrency_pool``
    identify the scarce resource a candidate draws from; routes that share a
    subscription declare the same pool id and must declare identical policies.
    """

    endpoint_id: str
    model_id: str
    adapter: BaseAdapter
    provider_type: ProviderType
    weight: float
    pricing: CandidatePricing
    routewise_pool: str
    quota_pool: str | None = None
    concurrency_pool: str | None = None
    quota_source: QuotaSource | None = None
    quota_policy: QuotaPolicy | None = None
    concurrency_policy: ConcurrencyPolicy | None = None


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

        provider_type = provider_type_for_adapter(adapter)
        config = adapter.config
        pricing = CandidatePricing.from_raw(
            _mapping_attr(config, "pricing") or {},
            context=f"{endpoint_id}.pricing",
        )
        routewise_pool = _optional_str_attr(config, "routewise_pool") or model_id

        quota_pool: str | None = None
        concurrency_pool: str | None = None
        quota_source: QuotaSource | None = None
        quota_policy: QuotaPolicy | None = None
        concurrency_policy: ConcurrencyPolicy | None = None

        if provider_type is ProviderType.QUOTA:
            quota_pool = _optional_str_attr(config, "quota_pool") or f"{model_id}:{endpoint_id}"
            quota_source_raw = _mapping_attr(config, "quota_source")
            quota_source = (
                QuotaSource.from_raw(quota_source_raw, context=f"{endpoint_id}.quota_source")
                if quota_source_raw is not None
                else None
            )
            quota_raw = _mapping_attr(config, "quota")
            if quota_raw is None:
                raise ValueError(
                    f"{endpoint_id}: provider_type 'quota' requires a route-level "
                    f"'quota:' block with at least 'limit' (model {model_id!r})"
                )
            quota_policy = QuotaPolicy.from_raw(quota_raw, context=f"{endpoint_id}.quota")

        if provider_type is ProviderType.CONCURRENCY:
            concurrency_pool = (
                _optional_str_attr(config, "concurrency_pool") or f"{model_id}:{endpoint_id}"
            )
            concurrency_raw = _mapping_attr(config, "concurrency")
            if concurrency_raw is None:
                raise ValueError(
                    f"{endpoint_id}: provider_type 'concurrency' requires a route-level "
                    f"'concurrency:' block with at least 'limit' (model {model_id!r})"
                )
            concurrency_policy = ConcurrencyPolicy.from_raw(
                concurrency_raw,
                context=f"{endpoint_id}.concurrency",
            )

        candidates.append(
            ProviderCandidate(
                endpoint_id=endpoint_id,
                model_id=model_id,
                adapter=adapter,
                provider_type=provider_type,
                weight=weight,
                pricing=pricing,
                routewise_pool=routewise_pool,
                quota_pool=quota_pool,
                concurrency_pool=concurrency_pool,
                quota_source=quota_source,
                quota_policy=quota_policy,
                concurrency_policy=concurrency_policy,
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


def provider_type_for_adapter(adapter: BaseAdapter) -> ProviderType:
    """Parse adapter ``provider_type``, defaulting an omitted value to on-demand."""
    raw = _optional_str_attr(adapter.config, "provider_type") or ProviderType.ON_DEMAND.value
    try:
        return ProviderType(raw)
    except ValueError as exc:
        allowed = ", ".join(provider_type.value for provider_type in ProviderType)
        raise ValueError(f"provider_type must be one of: {allowed}; got {raw!r}") from exc


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
