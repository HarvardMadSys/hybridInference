"""Structured scheduling results and the policy seam that produces them.

A policy answers one question -- which execution domain serves this request, and
is there a preferred target inside it -- and returns that answer as data. The
hybrid router dispatches the answer; a backend honors the target when it can.

The target carries two clearly separated identifiers:

``provider``
    A provider label, e.g. ``"zai"``. It can cover several endpoints, so a
    backend that receives one may choose among that provider's endpoints.
``endpoint_id``
    A canonical endpoint id, e.g. ``combo-model:zai-api``. Exactly one endpoint.

They are never interchangeable. ``target=None`` is the third case: no
preference was expressed, so the backend runs its own selection algorithm
(RouteWise, when the cloud side wraps it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from routing.protocols import RoutingRequestOptions

__all__ = ["BackendSelection", "FallbackAttempt", "RoutingDecision", "RoutingTarget"]


@dataclass(frozen=True, slots=True)
class FallbackAttempt:
    """One candidate a fallback plan wants tried, and the domain that owns it.

    A plan is a sequence of these rather than a sequence of backend names because
    the order that matters is the *route's*, not the domains': a route of
    ``L1, cloud, L2`` answers from the cloud when ``L1`` fails, which no ordering
    of two backend names can express. ``target`` names the exact endpoint, and
    the hybrid layer clears ``allow_fallback`` on the dispatch so the backend
    attempts that endpoint alone instead of re-walking its own range.
    """

    backend: str
    target: RoutingTarget | None = None


@dataclass(frozen=True, slots=True)
class RoutingTarget:
    """A preferred provider or endpoint, set only by scheduling policy.

    A policy target is a *preference*: when the chosen backend can serve it, it
    must be honored rather than re-sampled; when it cannot, the backend falls
    back to its own selection and reports that. A caller's hard pin stays on
    ``RoutingRequestOptions.pin_provider``, where the existing constraints --
    no fallback, ``ProviderPinError`` when absent -- continue to apply.
    """

    provider: str | None = None
    endpoint_id: str | None = None

    def __post_init__(self) -> None:
        provided = [field for field in (self.provider, self.endpoint_id) if field]
        if len(provided) != 1:
            raise ValueError(
                "RoutingTarget must set exactly one of provider= or endpoint_id=; "
                f"got provider={self.provider!r}, endpoint_id={self.endpoint_id!r}"
            )

    @property
    def is_endpoint(self) -> bool:
        """Return whether this target names one endpoint rather than a provider."""
        return bool(self.endpoint_id)

    def describe(self) -> str:
        """Return a compact label for routing metadata and logs."""
        if self.endpoint_id:
            return f"endpoint:{self.endpoint_id}"
        return f"provider:{self.provider}"


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """The result of one policy consultation.

    Args:
        backend: Name of the backend that should serve this request.
        target: Preferred provider or endpoint inside that backend, or ``None``
            to let the backend choose with its own algorithm.
    """

    backend: str
    target: RoutingTarget | None = None


@runtime_checkable
class BackendSelection(Protocol):
    """Scheduling policy: decide which backend serves one request, and where.

    Implementations return data only. They must not perform I/O, take locks, or
    dispatch the request themselves -- the hybrid router dispatches the backend
    they name, and a name it does not know fails loudly instead of silently
    falling back to a default side.
    """

    def select_backend(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> RoutingDecision:
        """Return the backend and preferred target for this request."""
        ...

    def fallback_backends(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        decision: RoutingDecision,
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> Sequence[str]:
        """Return backend names to try, in order, if the preferred one fails.

        The hybrid layer owns cross-domain fallback because neither execution
        domain can see the other's candidates. An empty sequence means the
        policy wants no cross-backend fallback for this request.

        Only attempts that produced no output are eligible: once a streaming
        response has yielded a chunk to the client it is committed, exactly as it
        is inside a single router.

        A policy that needs to name the *endpoint* of each attempt -- or to order
        attempts across domains rather than domain by domain -- also implements
        ``fallback_attempts()``, returning :class:`FallbackAttempt` entries in the
        order they should be tried. The hybrid router prefers that plan when the
        policy provides one and derives a domain-only plan from this method
        otherwise.
        """
        ...
