"""Nimbus routing strategy: the admission process beside the engine decides, the cloud takes the rest.

Self-registers via ``register_strategy("nimbus")`` at import time. Imported by
``routing.strategies.__init__`` to trigger registration.

A model that selects it has self-hosted routes that each declare
``admission: "nimbus"`` and cloud routes beside them (``routing.nimbus``). Its
parameters are what this gateway tells the process about the cloud:

==========================  =====================================================
``router_params``           Meaning
==========================  =====================================================
``cloud_ttft``              The cloud's first-token time, a line over the prompt's
                            length: the greedy router's ``cutoff.cloud_ttft``.
``cloud_max_input_tokens``  A longer prompt goes to no cloud route.
==========================  =====================================================

The first-token budget and the margin are not here: they are the process's own,
in its profile.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from routing.nimbus import NimbusRouter
from routing.strategies import register_strategy
from routing.strategies.greedy import CloudTtft  # noqa: TC001 - pydantic reads the field's type


class NimbusParams(BaseModel):
    """Parameters for the Nimbus routing strategy.

    Attributes:
        cloud_ttft: The cloud's first-token time for a prompt, which the
            admission process counts a request's cutoff from.
        cloud_max_input_tokens: A request whose estimated prompt is longer goes
            to no cloud route: the process is told the cloud cannot take it, and
            a request it declines all the same is answered as unservable. None
            for no limit.
    """

    model_config = {"extra": "forbid"}

    cloud_ttft: CloudTtft
    cloud_max_input_tokens: int | None = Field(default=None, ge=1)


register_strategy("nimbus")((NimbusRouter, NimbusParams))
