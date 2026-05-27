"""RouteWise current body-routing strategy.

Self-registers via ``register_strategy("routewise")`` at import time.

``RouteWiseParams`` mirrors ``routing.routewise.config.RouteWiseConfig``
field-for-field with the same defaults.  ``extra="forbid"`` rejects unknown
keys at boot, so a typo in ``models.yaml`` fails fast with a clear message.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from routing.routewise.router import RouteWiseRouter
from routing.strategies import register_strategy


class RouteWiseParams(BaseModel):
    """Parameters for the RouteWise routing strategy.

    Mirrors :class:`routing.routewise.config.RouteWiseConfig`.  When this
    model and the dataclass diverge, the test
    ``test_routewise_params_mirror_routewise_config_fields`` fails — keeping
    them in lockstep.
    """

    model_config = {"extra": "forbid"}

    budget_alpha: float = 0.75
    random_seed: int | None = None
    reference_api_price: dict[str, object] | None = None
    db_bootstrap_enabled: bool = True
    db_bootstrap_max_rows: int = 50_000
    stateful_tiers_single_worker_only: bool = True

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

    # Layer 2: latency-aware provider selection
    latency_slo_sec: float = 3.0
    latency_window_sec: float = 900.0
    latency_max_samples_per_profile: int = 5000
    latency_min_samples: int = 10
    latency_unprofiled_ttft_ms: float = 5000.0
    latency_hedge_mode: Literal["disabled", "probability_target"] = "disabled"

    # Canary rollout controls
    canary_enabled: bool = False
    canary_enabled_models: list[str] | None = None
    canary_traffic_fraction: float = 1.0


register_strategy("routewise")((RouteWiseRouter, RouteWiseParams))
