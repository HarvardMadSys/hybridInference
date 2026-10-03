"""Opt-in experimental Nimbus strategy, built through the standard registry."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from routing.nimbus import NimbusRouter
from routing.nimbus_policy import LocalProfile, PolicyName  # noqa: TC001 -- Pydantic schema
from routing.strategies import register_strategy


class NimbusParams(BaseModel):
    """One explicit local pool and one separately metered cloud endpoint.

    Cost units must match the experiment ledger. The profile and output estimate
    are deployment inputs, never future workload labels. Single-process use
    only; inject one NimbusPoolRegistry for all models sharing the local pool.
    """

    model_config = {"extra": "forbid", "allow_inf_nan": False}

    local_endpoint_id: str = Field(min_length=1)
    cloud_endpoint_id: str = Field(min_length=1)
    pool_id: str = Field(min_length=1)
    policy: PolicyName
    profile: LocalProfile
    ttft_slo_s: float = Field(gt=0)
    estimated_output_tokens: int = Field(ge=1)
    remote_input_cost_per_million: float = Field(ge=0)
    remote_output_cost_per_million: float = Field(ge=0)
    batch_window_s: float = Field(default=0.01, ge=0, le=1)
    max_pending: int = Field(default=1024, ge=1)
    cancel_grace_s: float = Field(default=5, ge=0)

    @model_validator(mode="after")
    def _distinct_endpoints(self) -> NimbusParams:
        if self.local_endpoint_id == self.cloud_endpoint_id:
            raise ValueError("local and cloud endpoints must differ")
        return self


register_strategy("nimbus")((NimbusRouter, NimbusParams))
