"""Scheduling policies: the decision layer above the execution domains.

A policy answers "which backend, and where inside it" without touching a
connection. The one shipped here is :class:`FixedPolicy`, which reproduces the
global decision the single ``FixedRouter`` used to make on its own: a weighted
draw across *every* candidate of a model -- local replicas, cloud endpoints and
each provider -- now expressed as a backend plus a preferred target.

The draw itself is not reimplemented. ``FixedPolicy`` asks the same
``FixedRouter`` a request would have hit anyway, through its side-effect-free
``select_adapter()``, so weights, dynamic overrides, circuit admission,
affinity and modality filtering all still come from one place. What the policy
adds is only the split: *which domain the draw landed in*, which the
single-component design could not name.

:meth:`FixedPolicy.fallback_attempts` hands the hybrid router the remaining
candidates in the order the route declares them, so the hybrid layer can
reproduce what the single component did: one loop over every candidate, local
and cloud interleaved exactly as they were registered.

RouteWise policies are deliberately absent. A model configured with
``router: routewise`` keeps its own router over its own full candidate pool for
now: that entry point is the pre-migration path, and the target architecture
puts RouteWise inside a ``CloudBackend`` instead. See the design doc's sections
on scope and on what the migration still owes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from routing.decisions import FallbackAttempt, RoutingDecision, RoutingTarget
from routing.endpoints import endpoint_id_for_adapter
from routing.prefill_load import estimate_prefill_tokens
from routing.routers import adapter_supports_modalities

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence

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
            May be a callable returning the current set, for a composition whose
            route table is edited while it runs; it is read once per decision.
        cloud_scope: Optional cloud range, used to decide whether the cloud side
            can serve a model at all. ``None`` means every non-local candidate.
            May also be a callable.

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
        local_scope: Collection[str] | Callable[[], Collection[str]] = (),
        cloud_scope: Collection[str] | Callable[[], Collection[str]] | None = None,
    ) -> None:
        self._compute = compute
        self._local_backend = local_backend
        self._cloud_backend = cloud_backend
        # Kept as given, not resolved once: a composition whose route table can be
        # edited supplies callables so both sides of the split move together, and
        # a policy holding a snapshot would classify an endpoint into the domain
        # it no longer belongs to. See _scopes().
        self._local_scope_spec = local_scope
        self._cloud_scope_spec = cloud_scope

    @property
    def compute(self) -> FixedRouter:
        """Return the router whose selection algorithm this policy consults."""
        return self._compute

    def _scopes(self) -> tuple[frozenset[str], frozenset[str] | None]:
        """Return the current local range and cloud range for this decision.

        Read once per public call and threaded through the helpers, so one
        decision cannot classify two candidates against two different splits.
        """
        local = self._local_scope_spec
        if callable(local):
            local = local()
        cloud = self._cloud_scope_spec
        if callable(cloud):
            cloud = cloud()
        return frozenset(local), (frozenset(cloud) if cloud is not None else None)

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
        local, cloud = self._scopes()
        options = routing_options
        pin = options.pin_provider if options is not None else None
        if pin:
            owner = self._domain_for_pin(model_id, pin, local, cloud)
            return RoutingDecision(
                backend=owner,
                target=RoutingTarget(provider=pin),
            )
        preferred = options.preferred_endpoint_id if options is not None else None
        if preferred:
            # An endpoint preference names one domain, so the policy does not
            # draw: drawing first and handing the target to whichever domain won
            # would deliver it to a backend that must ignore it, and the caller's
            # choice would be silently replaced by the weights.
            owner = self._domain_for_endpoint(model_id, preferred, local)
            if owner is not None:
                return RoutingDecision(
                    backend=owner,
                    target=RoutingTarget(endpoint_id=preferred),
                )
        adapter = self._compute.select_adapter(
            model_id,
            required_modalities=(
                routing_options.required_modalities if routing_options is not None else None
            ),
            prefill_tokens=estimate_prefill_tokens(
                messages,
                tools=params.get("tools"),
                response_format=params.get("response_format"),
            ),
        )
        if adapter is None:
            # No draw is possible; name the domain the model is configured for
            # so the backend raises the error the single-router path raised.
            return RoutingDecision(backend=self._default_backend(model_id, local, cloud))
        if self._is_local(adapter, local):
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

        Domain granularity only. :meth:`fallback_attempts` is what the hybrid
        router actually consumes, because the order that has to be preserved is
        the route's -- ``L1, cloud, L2`` answers from the cloud when ``L1``
        fails, which no ordering of two backend names can express. This method
        remains the protocol's contract for a policy that only classifies
        domains, and the hybrid layer falls back to it when a policy does not
        offer the richer plan.

        A caller pin suppresses the cross-domain step, because the pinned
        dispatch deliberately has no fallback at all.
        """
        local, cloud = self._scopes()
        pin = routing_options.pin_provider if routing_options is not None else None
        if pin:
            return ()
        other = (
            self._cloud_backend if decision.backend == self._local_backend else self._local_backend
        )
        return (other,) if self._domain_serves(other, model_id, local, cloud) else ()

    def fallback_attempts(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        decision: RoutingDecision,
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> tuple[FallbackAttempt, ...]:
        """Return the remaining candidates in the order the route declares them.

        This is the order the single ``FixedRouter`` walked, and reproducing it is
        why the hybrid layer owns the whole loop: a route of ``L1, cloud, L2``
        answered from the cloud when ``L1`` failed, and no ordering of two backend
        names can express that. Each attempt names its own endpoint, and the
        hybrid layer clears ``allow_fallback`` on the dispatch, so the sequence
        below is the sequence that happens rather than a plan a nested router may
        reorder.

        A caller pin has no fallback at all, so the plan is empty.
        """
        local, cloud = self._scopes()
        if routing_options is not None and routing_options.pin_provider:
            return ()
        required = (
            routing_options.required_modalities if routing_options is not None else frozenset()
        )
        primary_endpoint = decision.target.endpoint_id if decision.target is not None else None
        attempts: list[FallbackAttempt] = []
        for adapter, _weight in self._compute.eligible_adapters(model_id):
            if not adapter_supports_modalities(adapter, required):
                continue
            endpoint_id = endpoint_id_for_adapter(adapter)
            if endpoint_id == primary_endpoint:
                continue
            owns_local = self._is_local(adapter, local)
            declared = local if owns_local else cloud
            if declared is not None and not self._in_declared(adapter, declared):
                continue
            attempts.append(
                FallbackAttempt(
                    backend=self._local_backend if owns_local else self._cloud_backend,
                    target=RoutingTarget(endpoint_id=endpoint_id),
                )
            )
        return tuple(attempts)

    # -- classification -------------------------------------------------

    def _is_local(self, adapter: BaseAdapter, local: frozenset[str]) -> bool:
        """Return whether a candidate belongs to the local domain."""
        if not local:
            return False
        return self._in_declared(adapter, local)

    def _domain_for_endpoint(
        self,
        model_id: str,
        endpoint_id: str,
        local: frozenset[str],
    ) -> str | None:
        """Return the backend owning ``endpoint_id``, or None if it is unknown."""
        for adapter, _weight in self._compute.eligible_adapters(model_id):
            if endpoint_id_for_adapter(adapter) != endpoint_id:
                continue
            return self._local_backend if self._is_local(adapter, local) else self._cloud_backend
        return None

    def _domain_for_pin(
        self,
        model_id: str,
        provider: str,
        local: frozenset[str],
        cloud: frozenset[str] | None,
    ) -> str:
        """Return the backend that owns a pinned provider or endpoint."""
        for adapter, _weight in self._compute.eligible_adapters(model_id):
            endpoint_id = endpoint_id_for_adapter(adapter)
            if provider in (endpoint_id, adapter.config.provider):
                return (
                    self._local_backend if self._is_local(adapter, local) else self._cloud_backend
                )
        # Unknown to this model: keep the legacy disposition by naming the
        # domain whose router raises ProviderPinError for an absent pin.
        return self._default_backend(model_id, local, cloud)

    def _default_backend(
        self,
        model_id: str,
        local: frozenset[str],
        cloud: frozenset[str] | None,
    ) -> str:
        """Return the domain to route a model with no drawable candidate to."""
        if self._domain_serves(self._local_backend, model_id, local, cloud):
            return self._local_backend
        return self._cloud_backend

    def _domain_serves(
        self,
        backend_name: str,
        model_id: str,
        local: frozenset[str],
        cloud: frozenset[str] | None,
    ) -> bool:
        """Return whether ``backend_name`` has an eligible candidate for ``model_id``.

        A domain the composition declared empty cannot serve anything, whatever
        the model's routes still contain. Ignoring that -- as this class did while
        ``cloud_scope`` was stored and never read -- offers a request to a backend
        whose own range excludes every candidate, so the attempt can only fail
        with ``AllCircuitsOpenError``.
        """
        in_local_domain = backend_name == self._local_backend
        declared = local if in_local_domain else cloud
        if declared is not None and not declared:
            return False
        for adapter, _weight in self._compute.eligible_adapters(model_id):
            if self._is_local(adapter, local) is not in_local_domain:
                continue
            if declared is not None and not self._in_declared(adapter, declared):
                continue
            return True
        return False

    def _in_declared(self, adapter: BaseAdapter, declared: frozenset[str]) -> bool:
        """Return whether ``adapter`` is named by an explicit endpoint/provider set."""
        return bool(
            endpoint_id_for_adapter(adapter) in declared
            or getattr(adapter.config, "provider", None) in declared
        )
