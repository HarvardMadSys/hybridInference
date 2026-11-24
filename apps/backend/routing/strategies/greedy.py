"""Greedy (local-first) routing strategy.

Self-registers via ``register_strategy("greedy")`` at import time. Imported by
``apps/backend/routing/strategies/__init__.py`` for the side effect.

The parameters of :class:`routing.greedy.GreedyRouter`. ``cutoff`` makes a
model's requests wait for a local place until a deadline each request computes
from its prompt length, as the Cutoff baseline of the Nimbus project does, and
its six numbers are that project's profile fields, in the same units:

==================================  =================================
``router_params.cutoff``            Nimbus profile
==================================  =================================
``ttft_budget.fixed_ms``            ``slo.ttft_fixed_ms``
``ttft_budget.per_token_ms``        ``slo.ttft_per_token_ms``
``ttft_budget.threshold_tokens``    ``slo.ttft_threshold_tokens``
``cloud_ttft.fixed_ms``             ``provider.ttft_fixed_ms``
``cloud_ttft.per_input_token_ms``   ``provider.ttft_per_input_token_ms``
``margin_ms``                       ``provider.margin_ms``
==================================  =================================

Without ``cutoff`` the model spills over: a request that finds no free place
goes to the cloud at once. ``max_wait_seconds`` instead waits the same time for
every request, as the queue-wait offload does, and exists to migrate its configs.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from routing.greedy import GreedyRouter
from routing.strategies import register_strategy


class TtftBudget(BaseModel):
    """The first-token budget a prompt earns: a fixed part, plus a time per token past a threshold.

    Attributes:
        fixed_ms: Budget for any prompt.
        per_token_ms: Budget added for each prompt token past ``threshold_tokens``.
        threshold_tokens: Prompt length the per-token part starts after.
    """

    model_config = {"extra": "forbid"}

    fixed_ms: float = Field(ge=0, allow_inf_nan=False)
    per_token_ms: float = Field(ge=0, allow_inf_nan=False)
    threshold_tokens: int = Field(default=0, ge=0)


class CloudTtft(BaseModel):
    """The cloud's first-token time for an uncached prompt: fixed plus per input token.

    Attributes:
        fixed_ms: Time for any prompt.
        per_input_token_ms: Time added for each prompt token.
    """

    model_config = {"extra": "forbid"}

    fixed_ms: float = Field(ge=0, allow_inf_nan=False)
    per_input_token_ms: float = Field(ge=0, allow_inf_nan=False)


class CutoffParams(BaseModel):
    """What a request's Cutoff deadline is computed from (``routing.greedy.cutoff_deadline``).

    Attributes:
        ttft_budget: The first-token budget.
        cloud_ttft: The cloud's first-token time.
        margin_ms: Time left for this gateway's dispatch and the network before
            the cloud's first token. 30 ms, the Nimbus project's default.
    """

    model_config = {"extra": "forbid"}

    ttft_budget: TtftBudget
    cloud_ttft: CloudTtft
    margin_ms: float = Field(default=30.0, ge=0, allow_inf_nan=False)


class GreedyParams(BaseModel):
    """Parameters for the greedy (local-first) routing strategy.

    Attributes:
        engine_queue_limit: How many requests each of the model's local engines
            may have been sent without a first token before the rest wait. One,
            the default, is the Nimbus baselines' rule: the next request goes
            once every request ahead of it has its first token.
        cutoff: When given, a request that finds no free place waits until its
            own Cutoff deadline (see the module docstring). Without it, and
            without ``max_wait_seconds``, it goes to the cloud at once.
        max_wait_seconds: The same wait for every request, in seconds: the
            queue-wait offload's rule, kept for migrating its configs. Not with
            ``cutoff``.
        cloud_max_input_tokens: A request whose estimated prompt is longer goes
            to no cloud route; it waits for a local place. None for no limit.
    """

    model_config = {"extra": "forbid"}

    engine_queue_limit: int = Field(default=1, ge=1)
    cutoff: CutoffParams | None = None
    max_wait_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    cloud_max_input_tokens: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _one_wait(self) -> GreedyParams:
        if self.cutoff is not None and self.max_wait_seconds > 0:
            raise ValueError(
                "give cutoff or max_wait_seconds, not both: cutoff computes each "
                "request's wait, max_wait_seconds gives every request the same one"
            )
        return self


register_strategy("greedy")((GreedyRouter, GreedyParams))
