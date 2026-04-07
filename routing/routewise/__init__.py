"""RouteWise cost-aware routing package.

Exports:
    RouteWiseRouter  -- BaseRouter subclass with PD / LA-PD adapter selection.
    RouteWiseConfig  -- Dataclass holding policy parameters loaded from
                        ``config/routewise.yaml``.
    SubscriptionType -- Enum for quota / concurrency / API classification.
    load_routewise_config -- Loader helper for RouteWiseConfig.
    EMAOutputPredictor -- Production EMA output-token predictor.
    EMAState           -- Per-stream EMA tracking state.
    QuantilePrediction -- Quantile prediction dataclass.
    QuotaManager       -- Daily quota manager with shadow price computation.
    ConcurrencyManager -- Production concurrency slot manager (K=0 binary gate).
    ProviderProfile    -- Real-time latency profile for an API endpoint.
    SWRRSampler        -- Smooth weighted round-robin sampler.
    ShadowHedgeDecision -- Shadow hedge decision record.
    HedgedAdapter      -- Composite adapter that races primary vs backup.
    ProviderEventSink  -- Protocol for per-provider outcome reporting.
    compute_hedge_threshold -- SMART_ECONOMIC grid search for h*.
    survival_at        -- Empirical survival S(t) in SEPARATE mode.
    cdf_separate_at    -- Empirical CDF F(t) in SEPARATE mode.
    solve_provider_lp  -- LP solver for cost-minimization with tail constraints.
    solve_provider_lp_with_relaxation -- LP solver with progressive relaxation.
    pre_filter_providers -- Hard-filter providers by basic requirements.
"""

from .concurrency import ConcurrencyManager
from .config import RouteWiseConfig, load_routewise_config
from .hedging import (
    HedgedAdapter,
    ProviderEventSink,
    cdf_separate_at,
    compute_hedge_threshold,
    survival_at,
)
from .latency import ProviderProfile, ShadowHedgeDecision, SWRRSampler
from .lp_solver import (
    pre_filter_providers,
    solve_provider_lp,
    solve_provider_lp_with_relaxation,
)
from .predictor import EMAOutputPredictor, EMAState, QuantilePrediction
from .quota import QuotaManager
from .router import RouteWiseRouter, SubscriptionType

__all__ = [
    "ConcurrencyManager",
    "EMAOutputPredictor",
    "EMAState",
    "HedgedAdapter",
    "ProviderEventSink",
    "ProviderProfile",
    "QuantilePrediction",
    "QuotaManager",
    "RouteWiseConfig",
    "RouteWiseRouter",
    "SWRRSampler",
    "ShadowHedgeDecision",
    "SubscriptionType",
    "cdf_separate_at",
    "compute_hedge_threshold",
    "load_routewise_config",
    "pre_filter_providers",
    "solve_provider_lp",
    "solve_provider_lp_with_relaxation",
    "survival_at",
]
