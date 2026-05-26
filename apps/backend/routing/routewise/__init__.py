"""RouteWise cost-aware routing package.

Exports:
    RouteWiseRouter  -- BaseRouter subclass with current body-LP selection.
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
    HedgedAdapter      -- Composite adapter that races primary vs backup.
    CheckpointBackupDispatch -- Shared checkpoint backup dispatch dataclass.
    CheckpointBackupSelector -- Protocol for checkpoint-time backup selection.
    ProviderEventSink  -- Protocol for per-provider outcome reporting.
    solve_provider_lp  -- LP solver for cost-minimization with tail constraints.
    solve_provider_lp_with_relaxation -- LP solver with progressive relaxation.
    pre_filter_providers -- Hard-filter providers by basic requirements.
"""

from routewise.core import CheckpointBackupDispatch, CheckpointBackupSelector

from .candidates import CandidatePricing, ProviderCandidate, QuotaSource, SubscriptionType
from .concurrency import ConcurrencyManager
from .config import RouteWiseConfig, load_routewise_config
from .effective_cost import api_request_cost_usd, quota_shadow_price_usd
from .envelope import CostEnvelopeEstimator, CostEnvelopeSnapshot
from .hedging import HedgedAdapter, ProviderEventSink
from .latency import ProviderProfile, SWRRSampler
from .lp import LPCandidate, LPSolution, solve_cost_budgeted_mean_ttft
from .lp_solver import (
    pre_filter_providers,
    solve_provider_lp,
    solve_provider_lp_with_relaxation,
)
from .predictor import (
    BucketMeanOutputPredictor,
    BucketMeanPrediction,
    EMAOutputPredictor,
    EMAState,
    QuantilePrediction,
)
from .quota import QuotaManager
from .quota_snapshot import ProviderQuotaSnapshot, ProviderQuotaSnapshotStore
from .router import RouteWiseRouter

__all__ = [
    "BucketMeanOutputPredictor",
    "BucketMeanPrediction",
    "CandidatePricing",
    "CheckpointBackupDispatch",
    "CheckpointBackupSelector",
    "ConcurrencyManager",
    "CostEnvelopeEstimator",
    "CostEnvelopeSnapshot",
    "EMAOutputPredictor",
    "EMAState",
    "HedgedAdapter",
    "LPCandidate",
    "LPSolution",
    "ProviderCandidate",
    "ProviderEventSink",
    "ProviderProfile",
    "ProviderQuotaSnapshot",
    "ProviderQuotaSnapshotStore",
    "QuantilePrediction",
    "QuotaManager",
    "QuotaSource",
    "RouteWiseConfig",
    "RouteWiseRouter",
    "SWRRSampler",
    "SubscriptionType",
    "api_request_cost_usd",
    "load_routewise_config",
    "pre_filter_providers",
    "quota_shadow_price_usd",
    "solve_cost_budgeted_mean_ttft",
    "solve_provider_lp",
    "solve_provider_lp_with_relaxation",
]
