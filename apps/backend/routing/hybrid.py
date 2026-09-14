"""Hybrid router: one request surface over interchangeable execution backends.

``HybridRouter`` receives a request, asks an injected :class:`BackendSelection`
policy which backend should serve it -- and optionally which provider or
endpoint inside that backend it prefers -- then dispatches that decision. It is
the composition point for hybrid scheduling, not a scheduler itself: the policy
owns the *decision*, each backend owns its *execution domain*, and this class
owns only the seam between them.

Two things live here that neither domain can do for the other:

``Cross-backend fallback``
    A local fleet and a cloud provider set cannot see each other's candidates,
    so when the preferred backend fails before producing any output, this layer
    asks the policy which other backend to try. The preserved contract is the
    one the single ``FixedRouter`` used to provide: try the remaining candidates
    in policy order, re-raise the *preferred* attempt's error when everything
    fails, and merge every attempt into the routing metadata and the feedback.

``Feedback attribution``
    A request can now be served by more than one backend: attempt 1 on local,
    answer from cloud. Feedback is routed per observation, by the endpoint the
    observation names, so each attempt reaches the backend that actually ran it.
    The dispatch record is a tie-breaker for an endpoint two backends both claim
    and a cheap guard against mis-attribution; it is not an assumption that one
    request id maps to one backend.

Deliberate non-goals, matching the current design:

* No admission control, queueing or resource model. Those belong to whichever
  algorithm needs them, defined when that algorithm is added.
* No lifecycle ownership. Backends are injected already constructed, and the
  composition root that built them starts and stops them unless a backend
  explicitly opted into lifecycle management.
* No client-visible buffering of streaming responses. Chunks are forwarded one
  at a time; the pre-yield buffer below exists only to decide whether a failed
  attempt still has somewhere to fall back to.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from routing.decisions import BackendSelection, RoutingDecision, RoutingTarget
from routing.protocols import RoutingRequestOptions

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from routing.backends import RoutingBackend
    from routing.routers import RoutingObservation

__all__ = [
    "BackendSelection",
    "HybridRouter",
    "HybridRoutingError",
    "RoutingDecision",
    "RoutingTarget",
]

#: Cap on how many attempts one request may make across all backends. Two
#: domains is the shipped composition; the bound exists so a policy cannot turn
#: a failing request into an unbounded retry loop.
_MAX_BACKEND_ATTEMPTS = 4


class HybridRoutingError(RuntimeError):
    """Raised when a hybrid composition cannot dispatch the request it was given.

    Covers a policy naming a backend that was never injected and a policy
    returning a malformed decision. These are composition errors, not upstream
    failures, so they are never counted as upstream attempts.
    """


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


class HybridRouter:
    """Route each request through an injected policy to one of two backends.

    Args:
        policy: Scheduling policy consulted once per request, and again for the
            fallback plan when the preferred backend fails before output.
        local: Local execution backend (:class:`routing.backends.LocalBackend`).
        cloud: Cloud execution backend, typed against the
            :class:`routing.backends.CloudBackend` role. The concrete algorithm
            -- ``RouteWiseCloudBackend`` today -- is chosen by the composition
            root, which is what makes the cloud side replaceable.
        name: Identity used when this router reports itself as a backend.
        max_recorded_decisions: Bound on the ``request_id`` to backend map kept
            as a feedback-attribution tie-breaker.

    Both backends must implement :class:`routing.backends.RoutingBackend`; the
    two names must be distinct and must match what ``policy`` returns. A backend
    that declares its domain must declare the one it is passed as.
    """

    def __init__(
        self,
        *,
        policy: BackendSelection,
        local: RoutingBackend,
        cloud: RoutingBackend,
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
        # terminal feedback arrives. Ownership predicates alone cannot separate
        # two backends that both claim an endpoint, so the decision made at
        # dispatch time is the authoritative tie-breaker while it is available.
        self._max_recorded_decisions = max_recorded_decisions
        self._backend_decisions: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Policy
    # ------------------------------------------------------------------

    def select_decision(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> RoutingDecision:
        """Run the policy and return the decision it produced."""
        decision = self._policy.select_backend(
            model_id,
            messages,
            routing_options=routing_options,
            **params,
        )
        if not isinstance(decision, RoutingDecision):
            raise HybridRoutingError(
                "policy.select_backend() must return a RoutingDecision; "
                f"{type(self._policy).__name__} returned {type(decision).__name__}"
            )
        if decision.backend not in self._backends:
            raise HybridRoutingError(
                f"policy selected unknown backend {decision.backend!r}; "
                f"registered backends are {sorted(self._backends)}"
            )
        # Remembered here rather than at each dispatch site: the policy just
        # decided, and the feedback path needs that decision whether the request
        # is dispatched by this class or by a caller that resolves the backend
        # itself.
        self._remember_backend_decision(params.get("request_id"), decision.backend)
        return decision

    def fallback_backends(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        decision: RoutingDecision,
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> tuple[str, ...]:
        """Return the policy's cross-backend fallback order for this request."""
        plan = self._policy.fallback_backends(
            model_id,
            messages,
            decision,
            routing_options=routing_options,
            **params,
        )
        return tuple(
            backend_name
            for backend_name in plan
            if backend_name in self._backends and backend_name != decision.backend
        )

    def backend_for_request(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> RoutingBackend:
        """Return the backend the policy selected for this request."""
        decision = self.select_decision(
            model_id,
            messages,
            routing_options=routing_options,
            **params,
        )
        return self.backend(decision.backend)

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    async def chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Delegate one non-streaming request, falling back across backends."""
        decision = self.select_decision(
            model_id,
            messages,
            routing_options=routing_options,
            **params,
        )
        order = (
            decision.backend,
            *self._fallback_order(model_id, messages, decision, routing_options, params),
        )

        attempts: list[dict[str, Any]] = []
        preferred_error: BaseException | None = None
        preferred_endpoint: str | None = None
        seen: set[str] = set()
        for index, backend_name in enumerate(order):
            if backend_name in seen or index >= _MAX_BACKEND_ATTEMPTS:
                continue
            seen.add(backend_name)
            backend = self._backends[backend_name]
            target = decision.target if index == 0 else None
            try:
                response = await backend.chat_completion(
                    model_id,
                    messages,
                    routing_options=_options_for_backend(
                        routing_options, backend, target, model_id
                    ),
                    target=target,
                    **params,
                )
            except Exception as exc:
                attempt = _attempt_record(backend_name, exc)
                attempts.append(attempt)
                _append_failed_attempt(exc, attempt)
                if index == 0:
                    preferred_error = exc
                    preferred_endpoint = _resolved_endpoint(backend, target, model_id)
                continue
            _merge_attempt_history(response, attempts)
            _tag_backend(response, backend_name)
            # Resolve on the success path too: the metadata must say whether the
            # policy's target was in range regardless of which attempt answered.
            _tag_preference(
                response,
                decision.target,
                preferred_endpoint or _resolved_endpoint(backend, target, model_id),
            )
            return response
        _raise_after_attempts(preferred_error, attempts, model_id)

    def stream_chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> AsyncIterator[Any]:
        """Delegate one streaming request, falling back across backends.

        A response that has already yielded a chunk is committed: it is drained
        from that point on, and an error after it propagates instead of being
        re-attempted on another backend. Only an attempt that fails before
        producing output may fall back, which is the same rule the single-router
        path already applies.
        """
        decision = self.select_decision(
            model_id,
            messages,
            routing_options=routing_options,
            **params,
        )
        order = (
            decision.backend,
            *self._fallback_order(model_id, messages, decision, routing_options, params),
        )
        return _fallback_stream(
            self,
            decision,
            order,
            model_id,
            messages,
            routing_options,
            params,
        )

    def _fallback_order(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        decision: RoutingDecision,
        routing_options: RoutingRequestOptions | None,
        params: dict[str, Any],
    ) -> tuple[str, ...]:
        """Return the fallback backends that can actually serve ``model_id``."""
        return tuple(
            backend_name
            for backend_name in self.fallback_backends(
                model_id,
                messages,
                decision,
                routing_options=routing_options,
                **params,
            )
            if _serves(self._backends[backend_name], model_id)
        )

    # ------------------------------------------------------------------
    # Feedback and status
    # ------------------------------------------------------------------

    def record_observation(self, obs: RoutingObservation) -> None:
        """Deliver feedback to the backend that ran the attempt ``obs`` describes.

        Attribution follows the endpoint the observation names, because one
        request can now produce attempts on more than one backend: the local
        attempt's failure sample belongs to the local backend even when the
        request was answered from the cloud. The dispatch record for this
        ``request_id`` breaks a tie when two backends both claim the endpoint,
        and the terminal observation releases it so a late duplicate cannot be
        counted twice.
        """
        backend_name = self._resolve_feedback_backend(obs)
        if backend_name is None:
            return
        self._backends[backend_name].record_observation(obs)

    def _resolve_feedback_backend(self, obs: RoutingObservation) -> str | None:
        """Return the one backend that ran the attempt ``obs`` describes."""
        owners = [
            backend_name
            for backend_name, backend in self._backends.items()
            if backend.owns_observation(obs)
        ]
        if len(owners) == 1:
            self._release_decision(obs)
            return owners[0]
        request_id = getattr(obs, "request_id", None)
        if isinstance(request_id, str) and request_id:
            recorded = self._backend_decisions.get(request_id)
            if recorded is not None and recorded in owners:
                self._release_decision(obs)
                return recorded
            if recorded is not None and not owners:
                # The endpoint belongs to no backend here, so this observation
                # is a leftover the record cannot explain. Drop the record: the
                # request it described has concluded.
                self._backend_decisions.pop(request_id, None)
        # No owner, or several with nothing to separate them: sending the sample
        # to every claimant would double-count one attempt against two learning
        # states, which is worse than dropping it.
        return None

    def _release_decision(self, obs: RoutingObservation) -> None:
        """Forget the dispatch record once the request's feedback concluded."""
        if not getattr(obs, "terminal", True):
            return
        request_id = getattr(obs, "request_id", None)
        if isinstance(request_id, str) and request_id:
            self._backend_decisions.pop(request_id, None)

    def _remember_backend_decision(self, request_id: Any, backend_name: str) -> None:
        """Record which backend served a request until its feedback arrives."""
        if not isinstance(request_id, str) or not request_id:
            return
        decisions = self._backend_decisions
        if request_id not in decisions and len(decisions) >= self._max_recorded_decisions:
            # Bounded drop-oldest eviction: an observation that arrives after
            # eviction falls back to endpoint ownership instead of blocking.
            decisions.pop(next(iter(decisions)), None)
        decisions[request_id] = backend_name

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
        """Refresh each backend that exposes route-table refresh.

        Both backends of a shipped composition wrap the *same* router, so the
        wrapped router's own refresh can run once per backend. That is harmless
        because the operation is idempotent by contract, and each backend still
        has to run its own half: re-deriving the candidate range is what makes a
        route edit move an endpoint between domains.
        """
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


def _serves(backend: Any, model_id: str) -> bool:
    """Return whether ``backend`` can serve ``model_id`` at all."""
    serves = getattr(backend, "serves", None)
    if not callable(serves):
        return True
    try:
        return bool(serves(model_id))
    except Exception:  # pragma: no cover - a backend with an unusable range
        return True


async def _fallback_stream(
    router: HybridRouter,
    decision: RoutingDecision,
    order: Sequence[str],
    model_id: str,
    messages: list[dict[str, Any]],
    routing_options: RoutingRequestOptions | None,
    params: dict[str, Any],
) -> AsyncIterator[Any]:
    """Forward the first attempt that produces output, else try the next backend."""
    attempts: list[dict[str, Any]] = []
    preferred_error: BaseException | None = None
    preferred_endpoint: str | None = None
    seen: set[str] = set()
    for index, backend_name in enumerate(order):
        if backend_name in seen or index >= _MAX_BACKEND_ATTEMPTS:
            continue
        seen.add(backend_name)
        backend = router.backend(backend_name)
        target = decision.target if index == 0 else None
        stream = backend.stream_chat_completion(
            model_id,
            messages,
            routing_options=_options_for_backend(routing_options, backend, target, model_id),
            target=target,
            **params,
        )
        buffered: list[Any] = []
        aclose = getattr(stream, "aclose", None)
        # Set before the first forwarded chunk, not after the forwarding loop:
        # the ``except`` below spans the whole section, so an upstream failure
        # raised once output is already on the wire would otherwise be treated
        # as a failed attempt and re-attempted on the next backend, splicing two
        # answers into a single stream and swallowing the original error. The
        # single-router path already refuses that with its own
        # ``chunks_yielded`` guard.
        committed = False
        try:
            async for chunk in stream:
                buffered.append(chunk)
                break
            if buffered:
                # Output exists; this attempt is committed. Forward what was
                # held for the fallback decision, then stream the rest through.
                committed = True
                if attempts:
                    yield _attempt_history_chunk(
                        attempts,
                        target=decision.target,
                        resolved_endpoint=preferred_endpoint,
                    )
                for held in buffered:
                    yield _tag_backend_chunk(held, backend_name)
                async for chunk in stream:
                    yield _tag_backend_chunk(chunk, backend_name)
                return
            # A stream that ended without producing anything never reached the
            # client, so it is a failed attempt and the plan may continue.
            empty_error = RuntimeError("stream produced no output")
            attempts.append(_attempt_record(backend_name, empty_error))
            if index == 0:
                preferred_error = empty_error
                preferred_endpoint = _resolved_endpoint(backend, target, model_id)
        except Exception as exc:
            if committed:
                # The client already holds part of this answer. Restarting on
                # another backend would emit a stream no backend ever generated,
                # so the failure propagates exactly as the single-router path
                # propagates it.
                raise
            attempts.append(_attempt_record(backend_name, exc))
            if index == 0:
                preferred_error = exc
                preferred_endpoint = _resolved_endpoint(backend, target, model_id)
            continue
        finally:
            if callable(aclose):
                await aclose()
    _raise_after_attempts(preferred_error, attempts, model_id)


def _options_for_backend(
    routing_options: RoutingRequestOptions | None,
    backend: Any,
    target: RoutingTarget | None,
    model_id: str,
) -> RoutingRequestOptions | None:
    """Return request options carrying this backend's resolved target.

    The policy's preference travels as ``preferred_endpoint_id`` in
    ``RoutingRequestOptions`` rather than as a generation parameter, so it stays
    on the router-owned control surface: adapters never see it, and it cannot be
    confused with the caller's hard ``pin_provider``.
    """
    resolved = _resolved_endpoint(backend, target, model_id) if target is not None else None
    scope = _dispatch_scope(backend, model_id)
    if resolved is None and scope is None:
        return routing_options
    if routing_options is None:
        return RoutingRequestOptions(preferred_endpoint_id=resolved, endpoint_scope=scope)
    if (
        routing_options.preferred_endpoint_id == resolved
        and routing_options.endpoint_scope == scope
    ):
        return routing_options
    return replace(
        routing_options,
        preferred_endpoint_id=resolved,
        endpoint_scope=scope,
    )


def _dispatch_scope(backend: Any, model_id: str) -> frozenset[str] | None:
    """Return the candidate range the backend must stay inside for this model."""
    scope = getattr(backend, "dispatch_scope", None)
    if not callable(scope):
        return None
    try:
        return scope(model_id)
    except Exception:  # pragma: no cover - a backend with an unusable range
        return None


def _resolved_endpoint(backend: Any, target: RoutingTarget | None, model_id: str) -> str | None:
    """Return the endpoint a backend resolved ``target`` to, if any."""
    if target is None:
        return None
    resolve = getattr(backend, "resolve_target", None)
    if not callable(resolve):
        return None
    try:
        return resolve(target, model_id)
    except Exception:  # pragma: no cover - diagnostics must not fail a request
        return None


def _attempt_record(backend_name: str, exc: BaseException) -> dict[str, Any]:
    """Return the routing-metadata record for one failed attempt."""
    error_routing = getattr(exc, "_routing", None)
    endpoint = backend_name
    if isinstance(error_routing, dict) and error_routing.get("endpoint_id"):
        endpoint = str(error_routing["endpoint_id"])
    return {
        "backend": backend_name,
        "endpoint_id": endpoint,
        "error_type": exc.__class__.__name__,
        "error": str(exc),
    }


def _append_failed_attempt(exc: BaseException, attempt: dict[str, Any]) -> None:
    """Append ``attempt`` to the error's own ``_routing`` history, if it has one."""
    routing = getattr(exc, "_routing", None)
    if not isinstance(routing, dict):
        return
    existing = routing.get("failed_attempts")
    routing["failed_attempts"] = [
        *(existing if isinstance(existing, list) else []),
        attempt,
    ]


def _merge_attempt_history(response: dict[str, Any], attempts: list[dict[str, Any]]) -> None:
    """Merge every earlier attempt into the answer's routing metadata."""
    if not attempts:
        return
    routing = response.get("_routing")
    if not isinstance(routing, dict):
        routing = {}
        response["_routing"] = routing
    routing.setdefault("fallback", True)
    existing = routing.get("failed_attempts")
    routing["failed_attempts"] = [
        *(existing if isinstance(existing, list) else []),
        *attempts,
    ]


def _attempt_history_chunk(
    attempts: list[dict[str, Any]],
    *,
    target: RoutingTarget | None,
    resolved_endpoint: str | None,
) -> str:
    """Build the routing-metadata frame carrying earlier backends' attempts.

    Also carries the preferred target, so the stream's routing metadata explains
    why this fallback happened the same way the non-streaming answer does.
    """
    routing: dict[str, Any] = {"fallback": True, "failed_attempts": attempts}
    if resolved_endpoint is not None:
        routing["preferred_endpoint_id"] = resolved_endpoint
    if target is not None:
        routing["preferred_target"] = target.describe()
        routing["preference_in_range"] = resolved_endpoint is not None
    payload = {"choices": [], "_routing": routing}
    return f"data: {json.dumps(payload)}\n\n"


def _raise_after_attempts(
    preferred_error: BaseException | None,
    attempts: list[dict[str, Any]],
    model_id: str,
) -> None:
    """Re-raise the preferred attempt's error, as the single-router path does."""
    if preferred_error is not None:
        raise preferred_error
    raise HybridRoutingError(
        f"every configured backend failed for model {model_id}: "
        f"{[attempt['backend'] for attempt in attempts] or 'no backend attempted'}"
    )


def _tag_backend(response: dict[str, Any], backend_name: str) -> None:
    """Record the serving backend in a response's routing metadata."""
    routing = response.get("_routing")
    if isinstance(routing, dict):
        routing.setdefault("backend", backend_name)


def _tag_preference(
    response: dict[str, Any],
    target: RoutingTarget | None,
    resolved_endpoint: str | None,
) -> None:
    """Record the policy's target and whether this backend could honor it."""
    routing = response.get("_routing")
    if not isinstance(routing, dict):
        return
    if resolved_endpoint is not None:
        routing.setdefault("preferred_endpoint_id", resolved_endpoint)
    if target is not None:
        routing.setdefault("preferred_target", target.describe())
        routing.setdefault("preference_in_range", resolved_endpoint is not None)


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
