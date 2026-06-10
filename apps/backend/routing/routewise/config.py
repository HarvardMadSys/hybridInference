"""RouteWise policy configuration.

Defines tunable parameters for the current RouteWise body router, which uses
unified effective cost plus a cost-budgeted mean-TTFT LP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

LatencyHedgeMode = Literal["disabled", "probability_target"]
VALID_LATENCY_HEDGE_MODES = frozenset(("disabled", "probability_target"))


@dataclass
class RouteWiseConfig:
    """Per-model algorithm parameters for RouteWise cost-aware routing.

    Resource limits (quota windows, concurrency slots) are route-level
    configuration: each provider route declares its own ``quota:`` /
    ``concurrency:`` block in ``models.yaml``.

    Attributes:
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

    # Provider quota snapshot refresh cadence (router-level default; the
    # quota/concurrency limits themselves are route-level configuration).
    quota_snapshot_refresh_interval_sec: float = 60.0

    # Envelope (workload cost percentile) parameters
    shadow_price_window_hours: int = 24
    envelope_lower_percentile: float = 10.0
    envelope_upper_percentile: float = 90.0
    envelope_min_samples: int = 30

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
