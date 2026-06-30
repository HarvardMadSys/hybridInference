"""RouteWise policy configuration.

Defines tunable parameters for the RouteWise router, which uses
unified effective cost plus a cost-budgeted mean-TTFT LP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

LatencyHedgeMode = Literal["disabled", "probability_target"]
VALID_LATENCY_HEDGE_MODES = frozenset(("disabled", "probability_target"))

FallbackMode = Literal["policy", "strict"]
VALID_FALLBACK_MODES = frozenset(("policy", "strict"))


@dataclass
class RouteWiseConfig:
    """Per-model algorithm parameters for RouteWise cost-aware routing.

    Resource limits (quota windows, concurrency slots) are route-level
    configuration: each provider route declares its own ``quota:`` /
    ``concurrency:`` block in ``models.yaml``.

    Attributes:
        envelope_window_hours: Lookback window (hours) for the rolling
            envelope used by the quota shadow price.

        latency_slo_sec: Target SLO for latency-aware routing (seconds).
        latency_window_sec: Profile moving window duration (seconds).
        latency_history_prior_window_sec: Older successful latency lookback
            used as a cold-start prior when the live profile window is empty.
        latency_max_samples_per_profile: Maximum request outcomes retained per
            provider latency profile.
        latency_min_samples: Legacy minimum sample count exposed for runtime
            tuning; RouteWise provider selection uses the layered latency prior
            instead of gating cold endpoints out of the LP.
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
    envelope_window_hours: int = 24
    envelope_lower_percentile: float = 10.0
    envelope_upper_percentile: float = 90.0
    envelope_min_samples: int = 1
    # DB-bootstrap-only donor models: replay these models' historical rows
    # into THIS model's envelope (priced with this model's routes) so a model
    # with no traffic of its own can cold-start from a sibling serving the
    # same workload (e.g. minimax-fast borrowing minimax-m2.5). Runtime
    # envelope updates still come only from this model's own requests, so the
    # borrowed seed washes out of the rolling window as real traffic arrives.
    envelope_bootstrap_donor_models: list[str] | None = None

    # Latency layer: latency-aware provider selection
    latency_slo_sec: float = 3.0
    latency_window_sec: float = 900.0  # 15 min profile window
    latency_history_prior_window_sec: float = 86_400.0
    latency_max_samples_per_profile: int = 5000
    latency_min_samples: int = 10  # retained for runtime compatibility/diagnostics
    latency_unprofiled_ttft_ms: float = 5000.0
    latency_hedge_mode: LatencyHedgeMode = "disabled"

    # On-demand fallback policy when a selected provider fails mid-request.
    # Only RouteWise models honor this (FixedRouter has its own fallback path).
    #   "policy": re-solve the RouteWise LP over the remaining candidates and
    #       retry the request, excluding the failed endpoint (production default).
    #   "strict": no on-demand fallback -- a provider failure surfaces directly,
    #       so each request reflects a single provider outcome (benchmarking).
    fallback_mode: FallbackMode = "policy"

    # Optional active probing for cold or idle RouteWise endpoints. Probes
    # request one token and feed the same latency profiles used by live traffic.
    routewise_probe_enabled: bool = False
    routewise_probe_interval_sec: float = 300.0
    routewise_probe_timeout_sec: float = 30.0
    routewise_probe_idle_only: bool = True
    routewise_probe_idle_threshold_sec: float = 900.0
    routewise_probe_max_concurrency: int = 1

    # Guarded cache-aware cost adjustment. When enabled, on-demand candidates
    # can use the prefix-cache estimate as an effective-cost discount before the LP.
    # Default off; rotating key-pool endpoints are skipped until key_slot is
    # available in the cache scope.
    prefix_cache_cost_adjustment_enabled: bool = False

    def __post_init__(self) -> None:
        if self.latency_hedge_mode not in VALID_LATENCY_HEDGE_MODES:
            allowed = ", ".join(sorted(VALID_LATENCY_HEDGE_MODES))
            raise ValueError(
                f"Unsupported latency_hedge_mode {self.latency_hedge_mode!r}; "
                f"expected one of: {allowed}"
            )
        if self.fallback_mode not in VALID_FALLBACK_MODES:
            allowed = ", ".join(sorted(VALID_FALLBACK_MODES))
            raise ValueError(
                f"Unsupported fallback_mode {self.fallback_mode!r}; expected one of: {allowed}"
            )
