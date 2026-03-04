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
"""

from .config import RouteWiseConfig, load_routewise_config
from .predictor import EMAOutputPredictor, EMAState, QuantilePrediction
from .quota import QuotaManager
from .router import RouteWiseRouter, SubscriptionType

__all__ = [
    "EMAOutputPredictor",
    "EMAState",
    "QuotaManager",
    "QuantilePrediction",
    "RouteWiseConfig",
    "RouteWiseRouter",
    "SubscriptionType",
    "load_routewise_config",
]
