"""Explicit dependencies shared by router instances built at application startup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from routing.endpoint_health import EndpointHealthRegistry
    from routing.nimbus import NimbusPoolRegistry
    from routing.prefill_load import PrefillLoadTracker


@dataclass(frozen=True, slots=True)
class RouterBuildDependencies:
    """Process-scoped collaborators supplied to router strategy factories."""

    health_registry: EndpointHealthRegistry
    prefill_load: PrefillLoadTracker | None = None
    nimbus_pools: NimbusPoolRegistry | None = None
    nimbus_decision_sink: Callable[[dict[str, Any]], None] | None = None
