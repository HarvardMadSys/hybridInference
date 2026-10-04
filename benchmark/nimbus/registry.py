"""Private experiment binding over the production model-router registry.

The inherited registry still owns aliases, caching and request dispatch. Only
its private construction hook recognizes ``baseline_experiment``. Production
strategy registration, dependency types, defaults and Admin APIs are unchanged.
This hook is deliberately a test-harness seam, not a supported deployment API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, model_validator

from benchmark.nimbus.baselines import BaselinePolicy, BaselineProfile  # noqa: TC001
from benchmark.nimbus.router import BaselineRouter
from routing.model_router_registry import ModelRouterRegistry

if TYPE_CHECKING:
    from collections.abc import Callable

    from benchmark.nimbus.router import BaselinePoolRegistry
    from routing.protocols import RouterProtocol


class BaselineParams(BaseModel):
    """Validated single-process baseline inputs, without future output labels.

    ``max_pending`` bounds this router's undecided calls plus accepted unsent
    local waiters. Actual local calls use the shared pool's separate slot cap.
    """

    model_config = {"extra": "forbid", "allow_inf_nan": False}

    local_endpoint_id: str = Field(min_length=1)
    cloud_endpoint_id: str = Field(min_length=1)
    pool_id: str = Field(min_length=1)
    policy: BaselinePolicy
    profile: BaselineProfile
    local_max_inflight: int = Field(ge=1, strict=True)
    remote_input_cost_per_million: float = Field(ge=0)
    remote_output_cost_per_million: float = Field(ge=0)
    batch_window_s: float = Field(default=0.01, ge=0, le=1)
    max_pending: int = Field(default=4096, ge=1, strict=True)
    cancel_grace_s: float = Field(default=5, ge=0)
    max_candidates: int = Field(default=12, ge=1, le=16, strict=True)

    @model_validator(mode="after")
    def _distinct_endpoints(self) -> BaselineParams:
        if self.local_endpoint_id == self.cloud_endpoint_id:
            raise ValueError("local and cloud endpoints must differ")
        return self


class BaselineModelRouterRegistry(ModelRouterRegistry):
    """Keep normal registry behavior with one experiment-local construction case."""

    def __init__(
        self,
        *args: Any,
        baseline_pools: BaselinePoolRegistry | None = None,
        baseline_decision_sink: Callable[[dict[str, Any]], None] | None = None,
        **kwargs: Any,
    ) -> None:
        self._baseline_pools = baseline_pools
        self._baseline_decision_sink = baseline_decision_sink
        super().__init__(*args, **kwargs)

    def _build_router_for_spec(
        self,
        *,
        canonical_model_id: str,
        requested_model_id: str,
        name: str,
        params: dict[str, Any],
    ) -> RouterProtocol:
        if name != "baseline_experiment":
            return super()._build_router_for_spec(
                canonical_model_id=canonical_model_id,
                requested_model_id=requested_model_id,
                name=name,
                params=params,
            )
        validated = BaselineParams.model_validate(params)
        if self._shared_fixed is None:
            raise RuntimeError("baseline experiment requires the shared FixedRouter route table")
        router = BaselineRouter(
            validated,
            health_registry=self._shared_fixed.endpoint_health_registry,
            baseline_pools=self._baseline_pools,
            baseline_decision_sink=self._baseline_decision_sink,
        )
        router.attach_route_table(self._shared_fixed, model_scope={canonical_model_id})
        return router
