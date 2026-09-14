"""Hybrid router: one request surface over two interchangeable backends.

``HybridRouter`` receives a request, asks an injected :class:`BackendSelection`
policy which backend should serve it, and delegates the call. It is the
composition point for hybrid scheduling, not a scheduler itself: the policy owns
the decision, each backend owns its execution domain, and this class owns only
the seam between them.

Deliberate non-goals, matching the current design:

* No cross-backend retry. A backend's own fallback and hedging stay inside that
  backend; the hybrid layer never re-issues a request against the other side.
* No buffering of streaming responses. Chunks are forwarded one at a time and a
  closed consumer closes the downstream iterator it holds.
* No admission control, queueing or resource model. Those belong to whichever
  algorithm needs them, defined when that algorithm is added.
* No lifecycle ownership. Backends are injected already constructed, and the
  composition root that built them starts and stops them unless a backend
  explicitly opted into lifecycle management.

Feedback is delivered to exactly one backend. The backend recorded when the
policy chose for that request wins; otherwise the single backend whose
ownership predicate claims the endpoint does. An observation that resolves to
no backend or to two is dropped rather than broadcast, because a duplicated
sample corrupts the online state of a backend that never served the request.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from routing.backends import CloudBackend, RoutingBackend
    from routing.protocols import RoutingRequestOptions
    from routing.routers import RoutingObservation

__all__ = ["BackendSelection", "HybridRouter", "HybridRoutingError"]


def _check_backend_domain(backend: RoutingBackend, domain: str) -> None:
    """Reject a backend injected as the side it does not declare.

    Read with ``getattr`` so a minimal backend double that declares no domain
    stays usable; a backend that does declare one must declare the side it was
    passed as, which turns a swapped injection into a clear error instead of
    two backends answering for each other.
    """
    declares_local = getattr(backend, "is_local", None)
    declares_cloud = getattr(backend, "is_cloud", None)
    if domain == "local" and declares_cloud:
        raise ValueError(
            f"{type(backend).__name__} declares the cloud domain but was passed as local"
        )
    if domain == "cloud" and declares_local:
        raise ValueError(
            f"{type(backend).__name__} declares the local domain but was passed as cloud"
        )


class HybridRoutingError(RuntimeError):
    """Raised when a hybrid composition cannot dispatch the request it was given.

    Covers a policy naming a backend that was never injected, and an observation
    that names no backend this composition can attribute. These are composition
    errors, not upstream failures, so they are never retried across backends.
    """


@runtime_checkable
class BackendSelection(Protocol):
    """Scheduling policy: choose which backend serves one request.

    Implementations return a backend name. They must not perform I/O, take
    locks, or dispatch the request themselves -- the hybrid router dispatches
    the backend they name, and a name it does not know fails loudly instead of
    silently falling back to a default side.
    """

    def select_backend(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> str:
        """Return the name of the backend that should serve this request."""
        ...


class HybridRouter:
    """Route each request to one of two backends selected by an injected policy.

    Args:
        policy: Scheduling policy consulted once per request.
        local: Local execution backend (:class:`routing.backends.LocalBackend`).
        cloud: Cloud execution backend. ``HybridRouter`` only relies on the
            :class:`routing.backends.CloudBackend` role here; the concrete
            algorithm -- ``RouteWiseCloudBackend`` today -- is chosen by the
            composition root, which is what makes the cloud side replaceable.
        name: Identity used when this router reports itself as a backend.
        max_recorded_decisions: Bound on the ``request_id`` to backend map kept
            for feedback attribution.

    Both backends must implement :class:`routing.backends.RoutingBackend`; the
    two names must be distinct and must match what ``policy`` returns. A
    backend that declares its domain must declare the one it is passed as.
    """

    def __init__(
        self,
        *,
        policy: BackendSelection,
        local: RoutingBackend,
        cloud: CloudBackend,
        name: str = "hybrid",
        max_recorded_decisions: int = 4096,
    ) -> None:
        if max_recorded_decisions < 1:
            raise ValueError("max_recorded_decisions must be at least 1")
        _check_backend_domain(local, "local")
        _check_backend_domain(cloud, "cloud")
        self._policy = policy
        self._name = name
        self._backends: dict[str, RoutingBackend] = {}
        for backend in (local, cloud):
            self._register_backend(backend)
        self._started: set[str] = set()
        # Which backend served which request, kept only until that request's
        # feedback arrives. Ownership predicates alone cannot separate two
        # backends that both claim an endpoint, so the decision made at dispatch
        # time is the authoritative attribution when it is still available.
        self._max_recorded_decisions = max_recorded_decisions
        self._backend_decisions: dict[str, str] = {}

    def _register_backend(self, backend: RoutingBackend) -> None:
        """Register one backend, rejecting an empty or duplicate name."""
        backend_name = backend.name
        if not backend_name:
            raise ValueError(f"{type(backend).__name__} has an empty backend name")
        if backend_name in self._backends:
            raise ValueError(
                f"duplicate backend name {backend_name!r} in HybridRouter; "
                "local and cloud backends must be distinct"
            )
        self._backends[backend_name] = backend

    @property
    def name(self) -> str:
        """Return this router's identity inside a surrounding composition."""
        return self._name

    @property
    def policy(self) -> BackendSelection:
        """Return the injected scheduling policy."""
        return self._policy

    def backend(self, backend_name: str) -> RoutingBackend:
        """Return the backend registered under ``backend_name``."""
        try:
            return self._backends[backend_name]
        except KeyError:
            raise HybridRoutingError(
                f"HybridRouter has no backend named {backend_name!r}; "
                f"registered backends are {sorted(self._backends)}"
            ) from None

    def backends(self) -> tuple[RoutingBackend, ...]:
        """Return every registered backend in registration order."""
        return tuple(self._backends.values())

    def select_backend_name(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> str:
        """Run the policy and return the backend name it selected."""
        selected = self._policy.select_backend(
            model_id,
            messages,
            routing_options=routing_options,
            **params,
        )
        if selected not in self._backends:
            raise HybridRoutingError(
                f"policy selected unknown backend {selected!r}; "
                f"registered backends are {sorted(self._backends)}"
            )
        request_id = params.get("request_id")
        if isinstance(request_id, str) and request_id:
            self._remember_backend_decision(request_id, selected)
        return selected

    def _remember_backend_decision(self, request_id: str, backend_name: str) -> None:
        """Record which backend served a request until its feedback arrives."""
        decisions = self._backend_decisions
        if request_id not in decisions and len(decisions) >= self._max_recorded_decisions:
            # Bounded drop-oldest eviction: an observation that arrives after
            # eviction falls back to endpoint ownership instead of blocking.
            decisions.pop(next(iter(decisions)), None)
        decisions[request_id] = backend_name

    def _resolve_feedback_backend(self, obs: RoutingObservation) -> str | None:
        """Return the one backend that served ``obs``, or None if unsettled.

        The dispatch record outlives every non-terminal sample because one
        request emits several: the completions logger reports each failed
        attempt with ``terminal=False`` before the final ``terminal=True``
        result. Consuming the record on the first of those would leave the
        terminal result unattributable, so it is dropped only once the request
        concludes -- or by the bounded eviction that keeps the map finite.
        """
        request_id = getattr(obs, "request_id", None)
        if isinstance(request_id, str) and request_id:
            recorded = self._backend_decisions.get(request_id)
            if recorded is not None:
                if getattr(obs, "terminal", True):
                    self._backend_decisions.pop(request_id, None)
                return recorded
        owners = [
            backend_name
            for backend_name, backend in self._backends.items()
            if backend.owns_observation(obs)
        ]
        if len(owners) == 1:
            return owners[0]
        if not owners:
            return None
        # More than one backend claims this endpoint and no dispatch record
        # survives, so attribution is genuinely undecidable. Sending the sample
        # to every claimant would double-count one request against two
        # learning states; refresh has already judged dropping it safer.
        return None

    def backend_for_request(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> RoutingBackend:
        """Return the backend the policy selected for this request."""
        return self.backend(
            self.select_backend_name(
                model_id,
                messages,
                routing_options=routing_options,
                **params,
            )
        )

    async def chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Delegate one non-streaming request to the selected backend."""
        backend = self.backend_for_request(
            model_id,
            messages,
            routing_options=routing_options,
            **params,
        )
        response = await backend.chat_completion(
            model_id,
            messages,
            routing_options=routing_options,
            **params,
        )
        _tag_backend(response, backend.name)
        return response

    def stream_chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> AsyncIterator[Any]:
        """Delegate one streaming request to the selected backend.

        Returns a forwarding iterator rather than a pre-drained list. Closing
        the returned iterator (early consumer exit, cancellation, or
        ``aclose()``) propagates to the downstream iterator the backend handed
        over, so a router that owns background work per stream still unwinds it.
        """
        backend = self.backend_for_request(
            model_id,
            messages,
            routing_options=routing_options,
            **params,
        )
        inner = backend.stream_chat_completion(
            model_id,
            messages,
            routing_options=routing_options,
            **params,
        )
        return _closing_stream(inner, backend.name)

    def record_observation(self, obs: RoutingObservation) -> None:
        """Deliver feedback to exactly the backend that served the request.

        Attribution uses, in order: the backend recorded when the policy chose
        for this ``request_id``, then the single backend whose ownership
        predicate claims the endpoint. An observation that resolves to no
        backend, or to two, is dropped rather than broadcast: a duplicated
        sample corrupts the online state of a backend that never served the
        request, which is worse than a missing one.

        One request produces several observations -- per-attempt failures carry
        ``terminal=False`` -- so the dispatch record is only consumed by the
        terminal one.
        """
        backend_name = self._resolve_feedback_backend(obs)
        if backend_name is None:
            return
        self._backends[backend_name].record_observation(obs)

    def get_provider_status(self) -> dict[str, dict[str, Any]]:
        """Return the merged endpoint status of both backends."""
        merged: dict[str, dict[str, Any]] = {}
        for backend in self._backends.values():
            for endpoint_id, status in backend.get_provider_status().items():
                merged[endpoint_id] = status
        return merged

    def backend_status(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Return per-backend endpoint status for diagnostics."""
        return {
            backend_name: backend.get_provider_status()
            for backend_name, backend in self._backends.items()
        }

    def canonical_id(self, model_id: str) -> str:
        """Resolve ``model_id`` through the backends that declare the capability."""
        for backend in self._backends.values():
            resolver = getattr(backend, "canonical_id", None)
            if not callable(resolver):
                continue
            resolved = str(resolver(model_id))
            if resolved != model_id:
                return resolved
        return model_id

    def refresh_route_table(self) -> None:
        """Refresh each backend that exposes route-table refresh."""
        for backend in self._backends.values():
            refresh = getattr(backend, "refresh_route_table", None)
            if callable(refresh):
                refresh()

    async def start(self) -> bool:
        """Start the backends this router owns the lifecycle of.

        Returns:
            True when at least one backend was activated by this call. A backend
            that does not manage its own lifecycle, or that reports its router
            was already running under another owner, is not recorded as started
            here and is therefore never stopped here.
        """
        activated = False
        for backend_name, backend in self._backends.items():
            if backend_name in self._started:
                continue
            start = getattr(backend, "start", None)
            if not callable(start):
                continue
            if await start():
                self._started.add(backend_name)
                activated = True
        return activated

    async def stop(self) -> bool:
        """Stop the backends this router started, in reverse order.

        Returns:
            True when at least one backend was stopped by this call.
        """
        stopped = False
        for backend_name in sorted(self._started, reverse=True):
            backend = self._backends[backend_name]
            stop = getattr(backend, "stop", None)
            if callable(stop) and await stop():
                stopped = True
            self._started.discard(backend_name)
        return stopped


async def _closing_stream(
    inner: AsyncIterator[Any],
    backend_name: str,
) -> AsyncIterator[Any]:
    """Forward ``inner`` and close it when the consumer stops early."""
    try:
        async for chunk in inner:
            yield _tag_backend_chunk(chunk, backend_name)
    finally:
        aclose = getattr(inner, "aclose", None)
        if callable(aclose):
            await aclose()


def _tag_backend(response: dict[str, Any], backend_name: str) -> None:
    """Record the serving backend in a response's routing metadata."""
    routing = response.get("_routing")
    if isinstance(routing, dict):
        routing.setdefault("backend", backend_name)


def _tag_backend_chunk(chunk: Any, backend_name: str) -> Any:
    """Record the serving backend in an SSE chunk's routing payload.

    Only ``data:`` frames that already carry a ``_routing`` object are touched;
    everything else -- keep-alives, content deltas, the terminal ``[DONE]`` --
    is forwarded byte-for-byte.
    """
    if not isinstance(chunk, str) or not chunk.startswith("data: "):
        return chunk
    raw = chunk[6:].strip()
    if raw == "[DONE]":
        return chunk
    try:
        payload = json.loads(raw)
    except ValueError:
        return chunk
    if not isinstance(payload, dict):
        return chunk
    routing = payload.get("_routing")
    if not isinstance(routing, dict):
        return chunk
    routing.setdefault("backend", backend_name)
    return f"data: {json.dumps(payload)}\n\n"
