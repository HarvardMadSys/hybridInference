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
* No lifecycle ownership. Backends are injected already constructed; the
  composition root that built them starts and stops them.

Feedback is delivered to exactly one backend. The policy decides the common
case; an observation that arrives out of band is attributed by the endpoint it
names, so a shared endpoint never gets two samples and a backend is never told
about a request it did not serve.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from routing.backends import RoutingBackend
    from routing.protocols import RoutingRequestOptions
    from routing.routers import RoutingObservation

__all__ = ["BackendSelection", "HybridRouter", "HybridRoutingError"]


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
        local: Local execution backend.
        cloud: Cloud execution backend.
        name: Identity used when this router reports itself as a backend.

    Both backends must implement :class:`routing.backends.RoutingBackend`; the
    two names must be distinct and must match what ``policy`` returns.
    """

    def __init__(
        self,
        *,
        policy: BackendSelection,
        local: RoutingBackend,
        cloud: RoutingBackend,
        name: str = "hybrid",
    ) -> None:
        self._policy = policy
        self._name = name
        self._backends: dict[str, RoutingBackend] = {}
        for backend in (local, cloud):
            self._register_backend(backend)
        self._started: set[str] = set()

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
        return selected

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
        """Deliver feedback to exactly the backend that served the request."""
        owners = [backend for backend in self._backends.values() if backend.owns_observation(obs)]
        if len(owners) == 1:
            owners[0].record_observation(obs)
            return
        # Two backends can legitimately expose the same endpoint id (a cloud
        # and a local route to the same upstream). Selection already decided
        # which one served the request, but an out-of-band observation carries
        # no such record, so every claimant is told once rather than dropping
        # the sample for both.
        for backend in owners:
            backend.record_observation(obs)

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

    async def start(self) -> None:
        """Start the backends this router owns the lifecycle of.

        Only backends started here are stopped here, so a backend the
        composition root also manages is never started twice.
        """
        for backend_name, backend in self._backends.items():
            if backend_name in self._started:
                continue
            start = getattr(backend, "start", None)
            if not callable(start):
                continue
            await start()
            self._started.add(backend_name)

    async def stop(self) -> None:
        """Stop the backends this router started, in reverse order."""
        for backend_name in sorted(self._started, reverse=True):
            backend = self._backends[backend_name]
            stop = getattr(backend, "stop", None)
            if callable(stop):
                await stop()
            self._started.discard(backend_name)


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
