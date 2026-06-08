"""RouteWise policy configuration.

Defines tunable parameters for the current RouteWise body router, which uses
unified effective cost plus a cost-budgeted mean-TTFT LP.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from serving.utils.logging import get_logger

logger = get_logger(__name__)

LatencyHedgeMode = Literal["disabled", "probability_target"]
VALID_LATENCY_HEDGE_MODES = frozenset(("disabled", "probability_target"))


@dataclass
class RouteWiseConfig:
    """Policy parameters for RouteWise cost-aware routing.

    Attributes:
        daily_quota: Maximum requests per day for S_Q (quota) subscriptions.
        quota_monthly_fee: Monthly cost of the quota subscription (USD).
        reset_timezone: Timezone for daily quota reset.

        concurrency_enabled: Whether S_C (concurrency) routing is active.
            Disabled in Stage 1.
        concurrency_limit: Max concurrent requests for S_C subscriptions.
        concurrency_monthly_fee: Monthly cost of the concurrency subscription
            (USD).

        shadow_price_window_hours: Lookback window (hours) for the rolling
            envelope used by the quota shadow price.

        latency_slo_sec: Target SLO for latency-aware routing (seconds).
        latency_window_sec: Profile moving window duration (seconds).
        latency_max_samples_per_profile: Maximum request outcomes retained per
            provider latency profile.
        latency_min_samples: Minimum samples before LP warmup.
        budget_alpha: Interpolation factor for the LP cost budget:
            ``c_min + alpha * (c_max - c_min)``.
        db_bootstrap_enabled: Whether to warm RouteWise in-memory state from
            recent api_logs rows at startup.
        db_bootstrap_max_rows: Maximum api_logs rows replayed per RouteWise
            router during startup.
    """

    budget_alpha: float = 0.75
    random_seed: int | None = None
    reference_api_price: dict[str, Any] | None = None
    db_bootstrap_enabled: bool = True
    db_bootstrap_max_rows: int = 50_000

    # S_Q/S_C state is process-local in this first integration.  Keep the
    # single-worker guard enabled until quota/concurrency state is backed by a
    # shared store.
    stateful_providers_single_worker_only: bool = True

    # Output-length predictor
    output_default_tokens: float = 512.0
    output_min_bucket_samples: int = 3
    output_min_model_samples: int = 3
    output_min_global_samples: int = 3

    # S_Q quota parameters
    daily_quota: int = 5000
    quota_monthly_fee: float = 20.0
    reset_timezone: str = "UTC"
    quota_snapshot_refresh_interval_sec: float = 60.0

    # S_C concurrency parameters (Stage 2, disabled in Stage 1)
    concurrency_enabled: bool = False
    concurrency_limit: int = 8
    concurrency_monthly_fee: float = 25.0

    # Envelope (workload cost percentile) parameters
    shadow_price_window_hours: int = 24
    envelope_lower_percentile: float = 10.0
    envelope_upper_percentile: float = 90.0

    # Layer 2: Latency-aware provider selection
    latency_slo_sec: float = 3.0
    latency_window_sec: float = 900.0  # 15 min profile window
    latency_max_samples_per_profile: int = 5000
    latency_min_samples: int = 10  # warmup threshold
    latency_unprofiled_ttft_ms: float = 5000.0
    latency_hedge_mode: LatencyHedgeMode = "disabled"

    # Guarded cache-aware cost adjustment. When enabled, on-demand candidates
    # can use the prefix-cache estimate as an effective-cost discount before the LP.
    # Default off; rotating key-pool endpoints are skipped until key_slot is
    # available in the cache scope.
    prefix_cache_cost_adjustment_enabled: bool = False

    # Canary rollout controls
    canary_enabled: bool = False
    canary_enabled_models: list[str] | None = None  # None = all routewise models
    canary_traffic_fraction: float = 1.0  # 0.0-1.0

    def __post_init__(self) -> None:
        if self.latency_hedge_mode not in VALID_LATENCY_HEDGE_MODES:
            allowed = ", ".join(sorted(VALID_LATENCY_HEDGE_MODES))
            raise ValueError(
                f"Unsupported latency_hedge_mode {self.latency_hedge_mode!r}; "
                f"expected one of: {allowed}"
            )


def load_routewise_config(path: Path | None = None) -> RouteWiseConfig:
    """Load RouteWise configuration from a YAML file.

    If the file does not exist or cannot be parsed, returns a
    ``RouteWiseConfig`` with default values.

    Args:
        path: Path to the YAML configuration file.  When *None*, defaults
            to ``config/routewise.yaml`` relative to the project root.

    Returns:
        A populated ``RouteWiseConfig`` instance.
    """
    if path is None:
        path = Path("config/routewise.yaml")

    if not path.exists():
        logger.info("RouteWise config not found at %s; using defaults", path)
        return RouteWiseConfig()

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(
            "Failed to load RouteWise config from %s (%s); using defaults",
            path,
            exc,
        )
        return RouteWiseConfig()

    if not isinstance(raw, dict):
        logger.warning(
            "RouteWise config at %s must be a mapping at the root; got %s. Using defaults.",
            path,
            type(raw).__name__,
        )
        return RouteWiseConfig()

    section = raw.get("routewise", raw)
    if section is None:
        data: dict[str, Any] = {}
    elif isinstance(section, dict):
        data = section
    else:
        logger.warning(
            "RouteWise config section 'routewise' in %s must be a mapping; got %s. Using defaults.",
            path,
            type(section).__name__,
        )
        return RouteWiseConfig()

    # Flatten ADR nested sections (quota, concurrency, envelope, ...) into
    # the flat dataclass namespace so both flat and nested YAML work.
    _NESTED_MAP: dict[str, dict[str, str]] = {
        "quota": {
            "daily_quota": "daily_quota",
            "monthly_fee": "quota_monthly_fee",
            "reset_timezone": "reset_timezone",
            "snapshot_refresh_interval_sec": "quota_snapshot_refresh_interval_sec",
        },
        "concurrency": {
            "enabled": "concurrency_enabled",
            "limit": "concurrency_limit",
            "monthly_fee": "concurrency_monthly_fee",
        },
        "shadow_price": {
            "window_hours": "shadow_price_window_hours",
        },
        "envelope": {
            "bootstrap_window_hours": "shadow_price_window_hours",
            "window_hours": "shadow_price_window_hours",
            "lower_percentile": "envelope_lower_percentile",
            "upper_percentile": "envelope_upper_percentile",
        },
        "output_predictor": {
            "cold_start_tokens": "output_default_tokens",
            "default_tokens": "output_default_tokens",
            "min_bucket_samples": "output_min_bucket_samples",
            "min_model_samples": "output_min_model_samples",
            "min_global_samples": "output_min_global_samples",
        },
        "latency": {
            "slo_sec": "latency_slo_sec",
            "window_sec": "latency_window_sec",
            "max_samples": "latency_max_samples_per_profile",
            "min_samples": "latency_min_samples",
            "unprofiled_ttft_ms": "latency_unprofiled_ttft_ms",
            "hedge_mode": "latency_hedge_mode",
        },
        "canary": {
            "enabled": "canary_enabled",
            "enabled_models": "canary_enabled_models",
            "traffic_fraction": "canary_traffic_fraction",
        },
        "db_bootstrap": {
            "enabled": "db_bootstrap_enabled",
            "max_rows": "db_bootstrap_max_rows",
        },
    }

    flat: dict[str, Any] = {}
    valid_keys = {f.name for f in RouteWiseConfig.__dataclass_fields__.values()}
    nested_sections = set(_NESTED_MAP.keys())

    for k, v in data.items():
        if k in nested_sections and isinstance(v, dict):
            mapping = _NESTED_MAP[k]
            for sub_key, sub_val in v.items():
                flat_key = mapping.get(sub_key)
                if flat_key is not None:
                    flat[flat_key] = sub_val
                else:
                    logger.warning(
                        "RouteWise config: unrecognized key '%s.%s' in %s",
                        k,
                        sub_key,
                        path,
                    )
        elif k in valid_keys:
            flat[k] = v
        else:
            logger.warning("RouteWise config: unrecognized key '%s' in %s", k, path)

    cfg = RouteWiseConfig(**flat)
    logger.info("Loaded RouteWise config from %s", path)
    return cfg
