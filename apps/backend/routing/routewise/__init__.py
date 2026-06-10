"""RouteWise cost-aware routing package.

Exports:
    RouteWiseRouter  -- BaseRouter subclass with cost-budgeted provider selection.
    RouteWiseConfig  -- Dataclass holding per-model policy parameters, populated
                        from each model's ``router_params`` in ``config/models.yaml``.
    ProviderType -- Enum for on-demand / quota / concurrency provider categories.
    QuotaPool -- Provider-snapshot-backed quota pool (a queryable usage API
                 is the quota truth source; there is no local-counting kind).
    ConcurrencyManager -- Per-pool concurrency slot manager (K=0 binary gate).
    ProviderProfile    -- Real-time latency profile for an API endpoint.
    HedgedAdapter      -- Composite adapter that races primary vs backup.
    CheckpointBackupDispatch -- Shared checkpoint backup dispatch dataclass.
    CheckpointBackupSelector -- Protocol for checkpoint-time backup selection.
    ProviderEventSink  -- Protocol for per-provider outcome reporting.
"""

from routewise.core import CheckpointBackupDispatch, CheckpointBackupSelector

from .candidates import (
    CandidatePricing,
    ConcurrencyPolicy,
    ProviderCandidate,
    ProviderType,
    QuotaPolicy,
    QuotaSource,
)
from .concurrency import ConcurrencyManager
from .config import RouteWiseConfig
from .effective_cost import api_request_cost_usd, quota_shadow_price_usd
from .envelope import CostEnvelopeEstimator, CostEnvelopeSnapshot
from .hedging import HedgedAdapter, ProviderEventSink
from .latency import ProviderProfile
from .lp import LPCandidate, LPSolution, solve_cost_budgeted_mean_ttft
from .predictor import (
    BucketMeanOutputPredictor,
    BucketMeanPrediction,
)
from .quota import QuotaPool
from .quota_snapshot import ProviderQuotaSnapshot, ProviderQuotaSnapshotStore
from .router import RouteWiseRouter

__all__ = [
    "BucketMeanOutputPredictor",
    "BucketMeanPrediction",
    "CandidatePricing",
    "CheckpointBackupDispatch",
    "CheckpointBackupSelector",
    "ConcurrencyManager",
    "ConcurrencyPolicy",
    "CostEnvelopeEstimator",
    "CostEnvelopeSnapshot",
    "HedgedAdapter",
    "LPCandidate",
    "LPSolution",
    "ProviderCandidate",
    "ProviderEventSink",
    "ProviderProfile",
    "ProviderQuotaSnapshot",
    "ProviderQuotaSnapshotStore",
    "ProviderType",
    "QuotaPolicy",
    "QuotaPool",
    "QuotaSource",
    "RouteWiseConfig",
    "RouteWiseRouter",
    "api_request_cost_usd",
    "quota_shadow_price_usd",
    "solve_cost_budgeted_mean_ttft",
]
