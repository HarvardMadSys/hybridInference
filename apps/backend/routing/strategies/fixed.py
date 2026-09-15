"""Fixed (weighted-random) routing strategy.

Self-registers via ``register_strategy("fixed")`` at import time.  Imported
by ``apps/backend/routing/strategies/__init__.py`` for the side effect.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from routing.routers import FixedRouter
from routing.strategies import register_strategy


class FixedParams(BaseModel):
    """Parameters for the fixed (weighted-random) routing strategy.

    Attributes:
        local_fraction: Fraction of traffic biased toward local deployments
            (0.0-1.0).  Currently informational; per-route weights in
            ``models.yaml`` already encode the local/remote split, so
            ``FixedRouter`` does not consult this field today.  Kept in the
            schema for forward compatibility with hybrid weighting.
        hybrid_composition: Explicitly enable local/cloud composition for this
            model. Defaults to False until Fixed selection equivalence is proven.
    """

    model_config = {"extra": "forbid"}

    local_fraction: float = Field(default=0.5, ge=0.0, le=1.0)
    hybrid_composition: bool = False


register_strategy("fixed")((FixedRouter, FixedParams))
