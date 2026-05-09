"""RouteWise (cost-aware primal-dual) routing strategy.

Self-registers via ``register_strategy("routewise")`` at import time.

``RouteWiseParams`` mirrors ``routing.routewise.config.RouteWiseConfig``
field-for-field with the same defaults.  ``extra="forbid"`` rejects unknown
keys at boot, so a typo in ``models.yaml`` (``router_params: { daily_quotas: 5000 }``)
fails fast with a clear message rather than silently using the default.
"""

from __future__ import annotations

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

    # Decision rule + predictor
    decision_rule: str = "pd"
    predictor: str = "ema"
    risk_quantile: float = 0.10

    # S_Q quota parameters
    daily_quota: int = 5000
    quota_monthly_fee: float = 20.0
    reset_timezone: str = "UTC"

    # S_C concurrency parameters (Stage 2, disabled in Stage 1)
    concurrency_enabled: bool = False
    concurrency_limit: int = 8
    concurrency_monthly_fee: float = 25.0

    # Shadow price bounds
    shadow_price_L_seed: float = 0.001
    shadow_price_U_seed: float = 0.500
    shadow_price_adaptive: bool = True
    shadow_price_window_hours: int = 24
    shadow_price_min_ratio: int = 10

    # Layer 2: latency-aware provider selection
    latency_slo_sec: float = 3.0
    latency_target_cdf: float = 0.99
    latency_error_penalty: float = 0.0
    latency_window_sec: float = 900.0
    latency_min_samples: int = 10
    latency_lp_interval_sec: float = 60.0
    latency_swrr_alpha: float = 0.3
    latency_relaxation_factors: str = "1.2,1.5,2.0"
    latency_hedge_mode: str = "shadow"
    latency_hedge_cost_ratio: float = 0.1
    latency_hedge_dispatch_overhead_sec: float = 0.05

    # Canary rollout controls
    canary_enabled: bool = False
    canary_enabled_models: list[str] | None = None
    canary_traffic_fraction: float = 1.0


register_strategy("routewise")((RouteWiseRouter, RouteWiseParams))
