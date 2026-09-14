"""Scheduling policies: the decision layer above the execution domains.

A policy answers "which backend, and where inside it" without touching a
connection. The one shipped here is :class:`FixedPolicy`, which reproduces the
global decision the single ``FixedRouter`` used to make on its own: a weighted
draw across *every* candidate of a model -- local replicas, cloud endpoints and
each provider -- now expressed as a backend plus a preferred target.

The draw itself is not reimplemented. ``FixedPolicy`` asks the same
``FixedRouter`` a request would have hit anyway, through its side-effect-free
``select_adapter()``, so weights, dynamic overrides, circuit admission,
affinity, modality filtering and the prefill-aware selection all still come from
one place. What the policy adds is only the split: *which domain the draw landed
in*, which the single-component design could not name.

RouteWise policies are deliberately absent. A model configured with
``router: routewise`` keeps its own router over its own full candidate pool;
this layer does not narrow it. See the design doc's section on scope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from routing.decisions import RoutingDecision, RoutingTarget
from routing.endpoints import endpoint_id_for_adapter

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from routing.protocols import RoutingRequestOptions
    from routing.routers import FixedRouter
    from serving.adapters.base import BaseAdapter

__all__ = ["FixedPolicy"]


class FixedPolicy:
    """The ``fixed`` strategy: one global weighted draw, split by domain.

    Args:
        compute: The router holding the model's full weighted candidate set. Its
            selection algorithm is reused verbatim; this policy only classifies
            the adapter it returns.
        local_backend: Backend name for candidates inside ``local_scope``.
        cloud_backend: Backend name for every other candidate.
        local_scope: Canonical endpoint ids and/or provider labels that identify
            the local domain. Same explicit-set convention as the backends: an
            empty scope means "no local candidates", never "infer from a host".
        cloud_scope: Optional cloud range, used to decide whether the cloud side
            can serve a model at all. ``None`` means every non-local candidate.

    The policy never pins. It returns a *preferred* target, which the chosen
    backend honors when it can and otherwise replaces with its own selection; a
    caller's hard ``pin_provider`` stays on ``RoutingRequestOptions`` and keeps
    its existing semantics (no fallback, ``ProviderPinError`` when absent).
    """

    def __init__(
        self,
        *,
        compute: FixedRouter,
        local_backend: str = "local",
        cloud_backend: str = "cloud",
        local_scope: Collection[str] = (),
        cloud_scope: Collection[str] | None = None,
    ) -> None:
        self._compute = compute
        self._local_backend = local_backend
        self._cloud_backend = cloud_backend
        self._local_scope = frozenset(local_scope)
        self._cloud_scope = frozenset(cloud_scope) if cloud_scope is not None else None

    @property
    def compute(self) -> FixedRouter:
        """Return the router whose selection algorithm this policy consults."""
        return self._compute

    def select_backend(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> RoutingDecision:
        """Draw one global pick and report which domain it landed in.

        A caller pin short-circuits the draw: the pinned provider is the
        preferred target, and the backend that owns it serves the request. The
        pin's own error behavior is unchanged and still enforced by the backend
        router, not here.
        """
        pin = routing_options.pin_provider if routing_options is not None else None
        if pin:
            owner = self._domain_for_pin(model_id, pin)
            return RoutingDecision(
                backend=owner,
                target=RoutingTarget(provider=pin),
            )
        adapter = self._compute.select_adapter(
            model_id,
            required_modalities=(
                routing_options.required_modalities if routing_options is not None else None
            ),
        )
        if adapter is None:
            # No draw is possible; name the domain the model is configured for
            # so the backend raises the error the single-router path raised.
            return RoutingDecision(backend=self._default_backend(model_id))
        if self._is_local(adapter):
            return RoutingDecision(
                backend=self._local_backend,
                target=RoutingTarget(endpoint_id=endpoint_id_for_adapter(adapter)),
            )
        return RoutingDecision(
            backend=self._cloud_backend,
            target=RoutingTarget(endpoint_id=endpoint_id_for_adapter(adapter)),
        )

    def fallback_backends(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        decision: RoutingDecision,
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> Sequence[str]:
        """Return the other domain when it can serve this model.

        This is the behavior the single ``FixedRouter`` had by construction: its
        fallback loop walked the remaining adapters in route order, and those
        candidates could belong to either domain. The preferred domain is tried
        first, then the other one, and the preferred attempt's error is the one
        that surfaces if both fail.

        A caller pin suppresses the cross-domain step, because the pinned
        dispatch deliberately has no fallback at all.
        """
        pin = routing_options.pin_provider if routing_options is not None else None
        if pin:
            return ()
        other = (
            self._cloud_backend if decision.backend == self._local_backend else self._local_backend
        )
        return (other,) if self._domain_serves(other, model_id) else ()

    # -- classification -------------------------------------------------

    def _is_local(self, adapter: BaseAdapter) -> bool:
        """Return whether a candidate belongs to the local domain."""
        if not self._local_scope:
            return False
        return (
            endpoint_id_for_adapter(adapter) in self._local_scope
            or getattr(adapter.config, "provider", None) in self._local_scope
        )

    def _domain_for_pin(self, model_id: str, provider: str) -> str:
        """Return the backend that owns a pinned provider or endpoint."""
        for adapter, _weight in self._compute.eligible_adapters(model_id):
            endpoint_id = endpoint_id_for_adapter(adapter)
            if provider in (endpoint_id, adapter.config.provider):
                return self._local_backend if self._is_local(adapter) else self._cloud_backend
        # Unknown to this model: keep the legacy disposition by naming the
        # domain whose router raises ProviderPinError for an absent pin.
        return self._default_backend(model_id)

    def _default_backend(self, model_id: str) -> str:
        """Return the domain to route a model with no drawable candidate to."""
        if self._domain_serves(self._local_backend, model_id):
            return self._local_backend
        return self._cloud_backend

    def _domain_serves(self, backend_name: str, model_id: str) -> bool:
        """Return whether ``backend_name`` has an eligible candidate for ``model_id``."""
        local = backend_name == self._local_backend
        for adapter, _weight in self._compute.eligible_adapters(model_id):
            if self._is_local(adapter) is local:
                return True
        return False
