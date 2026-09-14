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
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from routing.decisions import BackendSelection, RoutingDecision, RoutingTarget
from routing.protocols import RoutingRequestOptions
from routing.routers import (
    AllCircuitsOpenError,
    TargetUnavailableError,
    select_surfaced_error,
)

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


class HybridRoutingError(RuntimeError):
    """Raised when a hybrid composition cannot dispatch the request it was given.

    Covers a policy naming a backend that was never injected and a policy
    returning a malformed decision. These are composition errors, not upstream
    failures, so they are never counted as upstream attempts.
    """


@dataclass(frozen=True, slots=True)
class _Attempt:
    """One dispatch this layer has planned.

    ``exact`` and ``domain_fallback`` describe two different questions, which is
    why they are not one flag:

    ``exact``
        May the wrapped router substitute another candidate for ``endpoint_id``?
        False only for the policy's own pick, whose target is a preference by
        contract. A planned candidate is True: substituting it reorders the plan
        and can dispatch an endpoint the plan has already left behind.
    ``domain_fallback``
        May the wrapped router walk the rest of its own range when this attempt
        fails? False when this layer owns the candidate order, because the
        router's own loop would try candidates out of order and some of them
        twice. True for a policy that only classifies domains, where in-domain
        order belongs to the backend.
    """

    backend: str
    endpoint_id: str | None
    exact: bool
    domain_fallback: bool


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
        """Delegate one non-streaming request, following the policy's plan."""
        decision = self.select_decision(
            model_id,
            messages,
            routing_options=routing_options,
            **params,
        )
        plan = self._attempt_plan(model_id, messages, decision, routing_options, params)
        preferred_endpoint = _resolved_endpoint(
            self._backends[decision.backend], decision.target, model_id
        )

        attempts: list[dict[str, Any]] = []
        errors: list[BaseException] = []
        dispatched: set[str] = set()
        for index, attempt in enumerate(plan):
            if attempt.endpoint_id is not None and attempt.endpoint_id in dispatched:
                # The primary attempt may substitute a candidate the plan has not
                # reached yet, so the plan can still name an endpoint that has
                # already run. The single router never retried an endpoint it had
                # dispatched; retrying here would double both the upstream request
                # and the failure it recorded.
                continue
            backend = self._backends[attempt.backend]
            try:
                response = await backend.chat_completion(
                    model_id,
                    messages,
                    routing_options=_attempt_options(routing_options, backend, attempt, model_id),
                    target=decision.target if index == 0 else None,
                    **params,
                )
            except (TargetUnavailableError, AllCircuitsOpenError) as exc:
                if not attempt.exact:
                    attempts.append(_attempt_record(attempt.backend, attempt.endpoint_id, exc))
                    errors.append(exc)
                continue
            except Exception as exc:
                attempts.append(_attempt_record(attempt.backend, attempt.endpoint_id, exc))
                errors.append(exc)
                _remember_dispatched(dispatched, attempt, exc)
                continue
            _merge_attempt_history(response, attempts)
            _tag_backend(response, attempt.backend)
            # Resolve on the success path too: the metadata must say whether the
            # policy's target was in range regardless of which attempt answered.
            _tag_preference(response, decision.target, preferred_endpoint)
            return response
        _raise_after_attempts(errors, attempts, model_id)

    def stream_chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> AsyncIterator[Any]:
        """Delegate one streaming request, following the policy's plan.

        An attempt that has produced client-visible output is committed: it is
        drained from that point on, and an error after it propagates instead of
        being re-attempted. Only an attempt that failed before the client could
        see anything hands over to the next candidate, which is the same rule the
        single-router path applies.
        """
        decision = self.select_decision(
            model_id,
            messages,
            routing_options=routing_options,
            **params,
        )
        plan = self._attempt_plan(model_id, messages, decision, routing_options, params)
        return _fallback_stream(
            self,
            decision,
            plan,
            model_id,
            messages,
            routing_options,
            params,
        )

    def _attempt_plan(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        decision: RoutingDecision,
        routing_options: RoutingRequestOptions | None,
        params: dict[str, Any],
    ) -> tuple[_Attempt, ...]:
        """Return every attempt for this request, in order.

        The policy supplies the candidate order; this method turns it into
        dispatches the hybrid layer can actually make, by resolving each
        candidate's endpoint inside the backend that owns it. A candidate that
        backend cannot reach is dropped rather than attempted, because attempting
        it would let the backend substitute a different endpoint.

        Two plan shapes are supported and they are not interchangeable. A
        per-endpoint plan (``fallback_attempts``) hands over the whole order, so
        this layer disables both the wrapped router's in-domain loop and its
        freedom to substitute the candidate it was given. A domain-only plan
        (``fallback_backends``) says nothing about candidates, so those attempts
        keep the backend's own selection and its in-domain order -- and their
        target is deliberately left unset rather than dropped.

        The primary attempt always stays, even with an unresolved target: a
        caller pin has to reach the backend that enforces it, and a policy target
        the backend cannot honor is that backend's own selection to make.
        """
        primary_backend = self._backends[decision.backend]
        primary_endpoint = _resolved_endpoint(primary_backend, decision.target, model_id)
        fallbacks, per_endpoint = self._requested_fallbacks(
            model_id, messages, decision, routing_options, params
        )
        plan: list[_Attempt] = [
            _Attempt(
                backend=decision.backend,
                endpoint_id=primary_endpoint,
                exact=False,
                domain_fallback=not per_endpoint,
            )
        ]

        for backend_name, target in fallbacks:
            backend = self._backends[backend_name]
            if not _serves(backend, model_id):
                continue
            if target is None:
                # Domain-level: this backend chooses, and may walk its own range.
                plan.append(
                    _Attempt(
                        backend=backend_name,
                        endpoint_id=None,
                        exact=False,
                        domain_fallback=True,
                    )
                )
                continue
            if target.endpoint_id is None:
                # A provider-wide target would let the backend re-sample its own
                # range, which is the reordering this plan exists to prevent.
                continue
            if target.endpoint_id == primary_endpoint:
                continue
            endpoint_id = _resolved_endpoint(backend, target, model_id)
            if endpoint_id is None:
                continue
            plan.append(
                _Attempt(
                    backend=backend_name,
                    endpoint_id=endpoint_id,
                    exact=True,
                    domain_fallback=False,
                )
            )
        return tuple(plan)

    def _requested_fallbacks(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        decision: RoutingDecision,
        routing_options: RoutingRequestOptions | None,
        params: dict[str, Any],
    ) -> tuple[tuple[tuple[str, RoutingTarget | None], ...], bool]:
        """Return the policy's candidate order and whether it names candidates.

        A policy that can name each candidate's endpoint implements
        ``fallback_attempts`` and gets the route's own order, interleaved across
        domains. One that only names domains falls back to
        ``fallback_backends``; those entries carry no target, and the boolean
        tells the caller to leave the backend's own selection and in-domain order
        alone rather than treating the absent target as an unreachable candidate.

        The richer plan is not filtered by domain: consecutive candidates can
        belong to the same one -- two local replicas in a row are the common case
        -- and the policy has already excluded the candidate it chose.
        """
        attempts = getattr(self._policy, "fallback_attempts", None)
        if callable(attempts):
            return (
                tuple(
                    (attempt.backend, attempt.target)
                    for attempt in attempts(
                        model_id,
                        messages,
                        decision,
                        routing_options=routing_options,
                        **params,
                    )
                    if attempt.backend in self._backends
                ),
                True,
            )
        return (
            tuple(
                (backend_name, None)
                for backend_name in self._fallback_order(
                    model_id, messages, decision, routing_options, params
                )
            ),
            False,
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
            if backend_name in self._backends
            and backend_name != decision.backend
            and _serves(self._backends[backend_name], model_id)
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
    plan: Sequence[_Attempt],
    model_id: str,
    messages: list[dict[str, Any]],
    routing_options: RoutingRequestOptions | None,
    params: dict[str, Any],
) -> AsyncIterator[Any]:
    """Forward the first attempt the client can see, else try the next candidate."""
    attempts: list[dict[str, Any]] = []
    errors: list[BaseException] = []
    preferred_endpoint = _resolved_endpoint(
        router.backend(decision.backend), decision.target, model_id
    )
    dispatched: set[str] = set()
    for index, attempt in enumerate(plan):
        if attempt.endpoint_id is not None and attempt.endpoint_id in dispatched:
            # See HybridRouter.chat_completion: the plan can name an endpoint a
            # substituted primary already ran, and it must not run twice.
            continue
        backend = router.backend(attempt.backend)
        stream = backend.stream_chat_completion(
            model_id,
            messages,
            routing_options=_attempt_options(routing_options, backend, attempt, model_id),
            target=decision.target if index == 0 else None,
            **params,
        )
        buffered: list[Any] = []
        aclose = getattr(stream, "aclose", None)
        # Commitment begins at the first chunk the *client* can see, which is not
        # the first chunk forwarded. Every backend emits a synthetic routing frame
        # before anything else (``{"choices": [], "_routing": {...}}``), and the
        # serving layer drops that frame before the response leaves the gateway,
        # so counting it as output would refuse a fallback that a failure before
        # the first visible byte is still entitled to take. The single-router path
        # draws the line in the same place: its ``chunks_yielded`` flag is set by
        # the adapter's chunks and never by its own synthetic frame.
        committed = False
        try:
            async for chunk in stream:
                buffered.append(chunk)
                break
            if buffered:
                # Output exists; forward what was held for the fallback decision,
                # then stream the rest through, recording commitment as it goes.
                if attempts:
                    yield _attempt_history_chunk(
                        attempts,
                        target=decision.target,
                        resolved_endpoint=preferred_endpoint,
                    )
                for held in buffered:
                    committed = committed or _is_client_visible(held)
                    yield _tag_backend_chunk(held, attempt.backend)
                async for chunk in stream:
                    committed = committed or _is_client_visible(chunk)
                    yield _tag_backend_chunk(chunk, attempt.backend)
                return
            # A stream that ended without producing anything never reached the
            # client, so it is a failed attempt and the plan may continue.
            empty_error = RuntimeError("stream produced no output")
            attempts.append(_attempt_record(attempt.backend, attempt.endpoint_id, empty_error))
            errors.append(empty_error)
        except (TargetUnavailableError, AllCircuitsOpenError) as exc:
            if committed:  # pragma: no cover - unavailability precedes any output
                raise
            if not attempt.exact:
                attempts.append(_attempt_record(attempt.backend, attempt.endpoint_id, exc))
                errors.append(exc)
        except Exception as exc:
            if committed:
                # The client already holds part of this answer. Restarting on
                # another candidate would emit a stream no backend ever generated,
                # so the failure propagates exactly as the single-router path
                # propagates it.
                raise
            attempts.append(_attempt_record(attempt.backend, attempt.endpoint_id, exc))
            errors.append(exc)
            _remember_dispatched(dispatched, attempt, exc)
        finally:
            if callable(aclose):
                await aclose()
    _raise_after_attempts(errors, attempts, model_id)


def _attempt_options(
    routing_options: RoutingRequestOptions | None,
    backend: Any,
    attempt: _Attempt,
    model_id: str,
) -> RoutingRequestOptions | None:
    """Return request options for one planned attempt.

    The target travels as ``preferred_endpoint_id`` in ``RoutingRequestOptions``
    rather than as a generation parameter, so it stays on the router-owned
    control surface: adapters never see it, and it cannot be confused with the
    caller's hard ``pin_provider``.

    The two flags come from the plan. ``allow_fallback`` is cleared when this
    layer owns the candidate order -- otherwise the backend walks its own range
    before the plan's next candidate, reordering attempts and dispatching some
    endpoints twice. ``require_target`` is set when the plan named one candidate,
    so a candidate that cannot be admitted comes back as
    :class:`TargetUnavailableError` and the plan moves on instead of the router
    quietly substituting a different endpoint.
    """
    scope = _dispatch_scope(backend, model_id)
    if routing_options is None:
        return RoutingRequestOptions(
            preferred_endpoint_id=attempt.endpoint_id,
            endpoint_scope=scope,
            allow_fallback=attempt.domain_fallback,
            require_target=attempt.exact,
        )
    return replace(
        routing_options,
        preferred_endpoint_id=attempt.endpoint_id,
        endpoint_scope=scope,
        allow_fallback=attempt.domain_fallback,
        require_target=attempt.exact,
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


def _remember_dispatched(
    dispatched: set[str],
    attempt: _Attempt,
    exc: BaseException,
) -> None:
    """Record the endpoint a failed attempt actually ran, if one can be named.

    The router reports the endpoint it dispatched in the error's ``_routing``
    block, and that can differ from the one the plan named: the primary attempt's
    target is a preference, so a target the router cannot admit is replaced by its
    own selection. The plan has to learn which endpoint that was, or it will
    dispatch it a second time.
    """
    routing = getattr(exc, "_routing", None)
    observed = routing.get("endpoint_id") if isinstance(routing, dict) else None
    if not isinstance(observed, str) or not observed:
        observed = attempt.endpoint_id
    if observed is not None:
        dispatched.add(observed)


def _attempt_record(
    backend_name: str,
    endpoint_id: str | None,
    exc: BaseException,
) -> dict[str, Any]:
    """Return the routing-metadata record for one failed attempt.

    The shape keeps the fields the single router's ``failed_attempt`` produced --
    ``provider``, ``endpoint_id``, ``error_type``, ``error`` -- because consumers
    read them: the DB log and the fallback diagnostic both resolve an attempt's
    provider from here, and a record without one loses the attribution rather
    than failing loudly. ``backend`` is added, since once one request can span two
    execution domains "which domain" is worth recording and no legacy field
    carries it.

    ``provider`` comes from the failing router's own ``_routing`` block, which is
    the only place this layer can read it: it holds no adapters. An attempt that
    failed before any router touched it -- an empty stream -- has no provider to
    report, and the caller falls back to the endpoint id as it always did.
    """
    error_routing = getattr(exc, "_routing", None)
    routing = error_routing if isinstance(error_routing, dict) else {}
    resolved = routing.get("endpoint_id") or endpoint_id or backend_name
    record: dict[str, Any] = {
        "endpoint_id": str(resolved),
        "error_type": exc.__class__.__name__,
        "error": str(exc),
    }
    provider = routing.get("provider")
    if provider:
        record["provider"] = str(provider)
    record["backend"] = backend_name
    return record


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
    errors: Sequence[BaseException],
    attempts: list[dict[str, Any]],
    model_id: str,
) -> None:
    """Raise the error the caller should be told about, once every attempt failed.

    Reuses the single-router rule (``select_surfaced_error``): the primary
    attempt's error by default, displaced only by a later attempt's
    request-describing status. Which failure the caller is told about therefore
    does not depend on which domain happened to be planned first.

    Every attempt is attached to whichever error is raised, so the error-log path
    can attribute each endpoint that was tried rather than only the one whose
    exception won.
    """
    if not errors:
        raise HybridRoutingError(
            f"every configured backend failed for model {model_id}: "
            f"{[attempt['backend'] for attempt in attempts] or 'no backend attempted'}"
        )
    surfaced = errors[select_surfaced_error(errors)]
    _attach_attempt_history(surfaced, attempts)
    raise surfaced


def _attach_attempt_history(exc: BaseException, attempts: list[dict[str, Any]]) -> None:
    """Record every attempt on the error the caller is told about.

    The error may already carry the attempt its own router made, so the records
    are merged by endpoint: the router saw one candidate, this layer saw them all,
    and the caller should see each endpoint exactly once.
    """
    if not attempts:
        return
    routing = getattr(exc, "_routing", None)
    if not isinstance(routing, dict):
        return
    existing = routing.get("failed_attempts")
    if not isinstance(existing, list):
        routing["failed_attempts"] = list(attempts)
        return
    known = {record.get("endpoint_id") for record in existing if isinstance(record, dict)}
    for record in attempts:
        endpoint_id = record.get("endpoint_id")
        if endpoint_id in known:
            continue
        existing.append(record)
        known.add(endpoint_id)


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


def _is_client_visible(chunk: Any) -> bool:
    """Return whether ``chunk`` carries anything the client will actually see.

    Mirrors the serving layer's own contract (``openai_chat_serializer.
    sanitize_chunk``): a frame is dropped before it reaches the client exactly
    when it carries ``_routing`` metadata, no ``choices`` and no ``usage``. That
    is the shape of the synthetic routing frame every backend emits first, and of
    the attempt-history frame this module injects.

    Nothing else is treated as invisible, so anything unrecognised -- a
    keep-alive, a malformed frame, a byte payload -- counts as committed. Being
    wrong in that direction only forgoes a fallback; being wrong the other way
    would splice two answers into one stream.
    """
    if not isinstance(chunk, str) or not chunk.startswith("data: "):
        return True
    raw = chunk[6:].strip()
    if raw == "[DONE]":
        return True
    try:
        payload = json.loads(raw)
    except ValueError:
        return True
    if not isinstance(payload, dict):
        return True
    if "_routing" not in payload:
        return True
    return bool(payload.get("choices")) or payload.get("usage") is not None


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
