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
    ProviderProfile    -- Real-time latency profile for an API endpoint.
    SWRRSampler        -- Smooth weighted round-robin sampler.
    ShadowHedgeDecision -- Shadow hedge decision record.
    solve_provider_lp  -- LP solver for cost-minimization with tail constraints.
    solve_provider_lp_with_relaxation -- LP solver with progressive relaxation.
    pre_filter_providers -- Hard-filter providers by basic requirements.
"""

from .config import RouteWiseConfig, load_routewise_config
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
    "EMAOutputPredictor",
    "EMAState",
    "ProviderProfile",
    "QuotaManager",
    "QuantilePrediction",
    "RouteWiseConfig",
    "RouteWiseRouter",
    "SWRRSampler",
    "ShadowHedgeDecision",
    "SubscriptionType",
    "load_routewise_config",
    "pre_filter_providers",
    "solve_provider_lp",
    "solve_provider_lp_with_relaxation",
]
