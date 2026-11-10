"""Explicit dependencies shared by router instances built at application startup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from routing.endpoint_health import EndpointHealthRegistry


@dataclass(frozen=True, slots=True)
class RouterBuildDependencies:
    """Process-scoped collaborators supplied to router strategy factories."""

    health_registry: EndpointHealthRegistry
