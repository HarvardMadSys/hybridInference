"""The greedy router: a model's own engines first, its cloud routes when they are full.

``router: greedy`` (``routing.strategies.greedy``) gives a model with both
self-hosted and cloud routes one rule: a request goes to a local engine while
the engine has a free place -- fewer than ``engine_queue_limit`` requests sent to
it without a first token yet -- and is never taken back once sent. A request that
finds no place:

- **Spillover** (no ``cutoff``): goes to the cloud at once.
- **Cutoff** (``cutoff``): waits in the gateway's engine queue until its own
  deadline, the latest moment the cloud can still give it a first token within
  its budget, and then goes to the cloud. The deadline is the Cutoff baseline's
  of the Nimbus project, computed per request from its prompt length.
- **Fixed wait** (``max_wait_seconds``): waits the same number of seconds for
  every request, as the queue-wait offload does; kept for migrating its configs.

The queue is the outbound limiter's (``serving.adapters.upstream_limiter``): this
router registers the model's local engines with their limit when it starts and
clears them when it stops, and the queue counts every request an engine is sent.

Each request is at most three steps, never a loop: local with its deadline, then
the cloud, then -- only when the request left the queue unsent and the cloud then
failed -- local again with no deadline, so a request a local engine could take
is never failed by this router. A local attempt that fails before any output goes
to the cloud once, as an ordinary fallback. Both pools wrap the shared
``FixedRouter``, scoped to the model's local and cloud routes, so selection,
health, prefill awareness, affinity, execution and accounting are its own, and
each step runs through :mod:`routing.pool_attempts`. The design is
``docs/agents/specs/2026-11-23-greedy-router-design.zh.md``.
"""

from __future__ import annotations

import json
import time
from contextlib import aclosing
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from routing.backends import FixedCloudBackend, LocalBackend
from routing.endpoints import endpoint_id_for_adapter
from routing.hybrid_attempt_history import merge_attempt_history, tag_backend
from routing.offload import OFFLOAD_LAST_RESORT, OFFLOAD_QUEUE_WAIT
from routing.pool_attempts import (
    AttemptLedger,
    PoolAttempt,
    StreamOutcome,
    chat_attempt,
    stream_attempt,
)
from routing.prefill_load import estimate_prefill_tokens
from routing.protocols import RoutingRequestOptions
from routing.routers import (
    AllCircuitsOpenError,
    _configured_context_window,
    _serves_caller,
    adapter_supports_modalities,
    engines_left_unsent,
)
from serving.adapters.key_pool import KeyPoolExhausted
from serving.adapters.upstream_limiter import (
    EngineQueueConflict,
    EngineQueueUnavailable,
    UpstreamQueueWaitExpired,
    UpstreamSaturated,
    get_upstream_limiter,
    is_local_endpoint,
)
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Collection

    from routing.routers import FixedRouter, RoutingObservation
    from routing.strategies.greedy import CutoffParams, GreedyParams

logger = get_logger(__name__)

__all__ = ["OFFLOAD_SPILLOVER", "GreedyRouter", "GreedyRoutingError", "cutoff_deadline"]

#: ``_routing["offload"]`` for a request that found no free place and went to the
#: cloud without waiting. The other reasons are the queue-wait offload's:
#: ``queue_wait`` for one that waited until its deadline, ``last_resort`` for one
#: no local engine could take at all.
OFFLOAD_SPILLOVER = "spillover"

_LOCAL = "local"
_CLOUD = "cloud"


class GreedyRoutingError(RuntimeError):
    """The greedy router cannot route a request it was given.

    A composition or configuration problem -- the router has no route table yet,
    or its model lacks a local or a cloud route -- never an upstream failure.
    """


def cutoff_deadline(cutoff: CutoffParams, prompt_tokens: int, arrival: float) -> float | None:
    """Return a request's Cutoff deadline, as a ``time.monotonic()`` time, or None.

    The latest moment the request can still go to the cloud and have its first
    token within its budget: its arrival, plus the first-token budget its prompt
    earns, less the cloud's first-token time for that prompt uncached and the
    margin for this gateway's dispatch and the network. The prompt counts whole,
    whatever is cached. A cloud that cannot answer within the budget at all gives
    no deadline: the request waits for its local engine.
    """
    budget = cutoff.ttft_budget
    budget_ms = budget.fixed_ms + budget.per_token_ms * max(
        0, prompt_tokens - budget.threshold_tokens
    )
    cloud_ms = cutoff.cloud_ttft.fixed_ms + cutoff.cloud_ttft.per_input_token_ms * prompt_tokens
    if cloud_ms > budget_ms:
        return None
    return arrival + (budget_ms - cloud_ms - cutoff.margin_ms) / 1000.0


def _fits_window(adapter: Any, prompt_tokens: int) -> bool:
    """Whether a prompt of the estimated length fits the route's configured window.

    A route with no configured window is taken to fit, as the shared router
    takes it: it learns otherwise only from the upstream's refusal.
    """
    window = _configured_context_window(adapter)
    return window is None or prompt_tokens <= window


def _left_unsent(exc: BaseException) -> bool:
    """Whether an attempt left the engine queue at its deadline, sending nothing."""
    return isinstance(exc, UpstreamQueueWaitExpired)


#: What a route raises without sending anything upstream: no outbound place in
#: this gateway's own limiter, or no key the caller may use.
_NOTHING_SENT = (UpstreamSaturated, KeyPoolExhausted)


def _sent_to(exc: BaseException, record: dict[str, Any]) -> str | None:
    """Return the cloud route a failed cloud step sent the request to, or None.

    ``record`` is the ledger's record of the step. The pool walks its routes and
    raises one route's error, so an error raised before anything was sent
    (:data:`_NOTHING_SENT`) can still hide a route that was sent the request. The
    error's routing block lists every route the pool tried, and marks
    ``uncharged`` those that ended without anything charged to the route.
    """
    if not isinstance(exc, _NOTHING_SENT):
        endpoint = record.get("endpoint_id")
        return str(endpoint) if endpoint else _CLOUD
    routing = getattr(exc, "_routing", None)
    tried = routing.get("failed_attempts") if isinstance(routing, dict) else None
    for attempt in tried if isinstance(tried, list) else ():
        if (
            isinstance(attempt, dict)
            and not attempt.get("uncharged")
            and attempt.get("endpoint_id")
        ):
            return str(attempt["endpoint_id"])
    return None


def _caller_owns_dispatch(options: RoutingRequestOptions | None) -> bool:
    """Whether the caller already chose the endpoint, so there is no split to make.

    A pin, a binding, a required target or a plan that owns the candidate order
    leaves nothing for this router to decide; the shared router runs it as is,
    and a registered engine still counts it.
    """
    if options is None:
        return False
    return bool(
        options.pin_provider
        or options.bound_endpoint is not None
        or options.require_target
        or not options.allow_fallback
    )


@dataclass(frozen=True, slots=True)
class _Plan:
    """One request's steps, decided when it arrives.

    ``local`` and ``cloud`` are each step's options, or None when that side
    cannot take the request at all. ``local_retry`` is the local step without a
    deadline. ``spillover`` is whether the deadline had passed on arrival, which
    names the reason a request that left the queue goes to the cloud. The rest
    say why a range is narrower than its pool's, for the error a request that
    neither side can take is told: ``capped``, its prompt is over
    ``cloud_max_input_tokens`` and the cloud had routes; ``too_long``, the routes
    whose configured window cannot fit it; ``prompt_tokens``, its estimate.
    """

    local: RoutingRequestOptions | None
    local_retry: RoutingRequestOptions | None
    cloud: RoutingRequestOptions | None
    spillover: bool
    capped: bool = False
    too_long: frozenset[str] = frozenset()
    prompt_tokens: int = 0


def _frame(routing: dict[str, Any]) -> str:
    """Build a synthetic routing frame, as the shared router's ``routing_chunk`` does."""
    return f"data: {json.dumps({'choices': [], '_routing': routing})}\n\n"


@dataclass(frozen=True, slots=True)
class _Step:
    """What one step's routing metadata says about the steps before it.

    The serving layer reads a routing frame flagged ``fallback`` as the start of
    a new upstream and lets its keys replace the ones before it, and a pool that
    moves on to another of its routes sends such a frame of its own. So a step
    restates its history in the frame it opens with, on every frame its pool
    sends, and on the response or the error it ends with.

    Attributes:
        after_local: Whether the local step ran before this one, which makes this
            step a new attempt.
        earlier: The failed attempts of the steps before this one.
        offload: Why the request left its engine (``_routing["offload"]``), or
            None when it did not.
        offload_endpoint_id: The cloud route the request was sent to and that
            failed, when this is the local step after it.
    """

    after_local: bool = False
    earlier: tuple[dict[str, Any], ...] = ()
    offload: str | None = None
    offload_endpoint_id: str | None = None

    def mark(self, routing: dict[str, Any]) -> None:
        """Mark a routing block as offloaded, as the shared router marks its own.

        ``setdefault``, so a block built further upstream keeps what it says.
        """
        if self.offload is None:
            return
        routing.setdefault("offload", self.offload)
        if self.offload_endpoint_id is not None:
            routing.setdefault("offload_endpoint_id", self.offload_endpoint_id)

    def lead_frame(self) -> str | None:
        """Return the routing frame this step opens with, or None when it has nothing to say."""
        routing: dict[str, Any] = {}
        if self.after_local:
            routing["fallback"] = True
        if self.earlier:
            routing["failed_attempts"] = list(self.earlier)
        self.mark(routing)
        return _frame(routing) if routing else None

    def stamp(self, routing: dict[str, Any]) -> None:
        """Restate this step's history on a routing frame its pool sent."""
        self.mark(routing)
        if self.earlier and routing.get("fallback"):
            existing = routing.get("failed_attempts")
            routing["failed_attempts"] = [
                *self.earlier,
                *(existing if isinstance(existing, list) else []),
            ]

    def served(self, response: dict[str, Any], backend: str) -> dict[str, Any]:
        """Record on a response which step answered it, and why it left its engine."""
        merge_attempt_history(response, list(self.earlier))
        routing = response.get("_routing")
        if not isinstance(routing, dict):
            routing = {}
            response["_routing"] = routing
        if self.after_local:
            routing["fallback"] = True
        self.mark(routing)
        tag_backend(response, backend)
        return response


class GreedyRouter:
    """Route a model's requests to its local engines first, and to the cloud when full.

    Built by the strategy registry (``build_router``) with its parameters, then
    handed the shared ``FixedRouter`` by ``ModelRouterRegistry`` through
    :meth:`attach_route_table`, scoped to its one model. Started and stopped as a
    ``ManagedRouter``: starting registers the model's engine queues, stopping
    clears them.

    Args:
        params: The model's validated ``router_params``.
        health_registry: Accepted for the registry's dependency injection; the
            shared router carries the endpoint health this router's pools use.
    """

    def __init__(self, *, params: GreedyParams, health_registry: Any = None) -> None:
        del health_registry
        self._params = params
        self._fixed: FixedRouter | None = None
        self._model_id: str | None = None
        self._local: LocalBackend | None = None
        self._cloud: FixedCloudBackend | None = None
        self._started = False
        self._stopped = False
        # The limiter's token for this router's registration (set_engine_queues).
        self._registration: object | None = None
        # The engines (base URLs) the last registration that succeeded named: the
        # only self-hosted engines a request is sent to (see _register).
        self._queued: frozenset[str] = frozenset()

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    @property
    def params(self) -> GreedyParams:
        """Return this router's parameters."""
        return self._params

    @property
    def model_id(self) -> str | None:
        """Return the one model this router serves, once attached."""
        return self._model_id

    def attach_route_table(
        self,
        route_table: Any,
        *,
        model_scope: Collection[str] | None = None,
    ) -> None:
        """Bind the shared ``FixedRouter`` and the one model this router serves.

        Builds the two pools over it: the model's self-hosted routes and the rest,
        each read from the live route table on every request, so a route edit
        moves an endpoint between them without a rebuild. A route counts as local
        by the gateway's one classification (``is_local_endpoint``), the one the
        engine queue itself uses.

        Raises:
            GreedyRoutingError: ``model_scope`` does not name exactly one model, or
                the model lacks a self-hosted or a cloud route.
        """
        scope = frozenset(model_scope or ())
        if len(scope) != 1:
            raise GreedyRoutingError(
                f"router: greedy serves exactly one model and was given {sorted(scope)}"
            )
        (model_id,) = scope
        self._fixed = route_table
        self._model_id = model_id
        if not self._local_scope():
            raise GreedyRoutingError(
                f"router: greedy needs a self-hosted route for model {model_id!r}"
            )
        if not self._cloud_scope():
            raise GreedyRoutingError(f"router: greedy needs a cloud route for model {model_id!r}")
        self._local = LocalBackend(
            route_table, endpoint_scope=self._local_scope, model_scope={model_id}
        )
        self._cloud = FixedCloudBackend(
            route_table, endpoint_scope=self._cloud_scope, model_scope={model_id}
        )
        if self._started:
            self._register()

    def _local_adapters(self) -> list[Any]:
        """Return the model's routes to a self-hosted engine, weight 0 included."""
        route = self._fixed.routes.get(self._model_id) if self._fixed is not None else None
        if route is None or not route.adapters:
            return []
        return [
            adapter
            for adapter, _weight in route.adapters
            if is_local_endpoint(
                getattr(adapter.config, "base_url", None),
                deployment_type=getattr(adapter.config, "deployment_type", "auto"),
            )
        ]

    def _route_endpoints(self) -> frozenset[str]:
        """Return every endpoint id on the model's route now."""
        route = self._fixed.routes.get(self._model_id) if self._fixed is not None else None
        if route is None or not route.adapters:
            return frozenset()
        return frozenset(
            str(adapter.config.endpoint_id)
            for adapter, _weight in route.adapters
            if getattr(adapter.config, "endpoint_id", None)
        )

    def _local_scope(self) -> frozenset[str]:
        """Return the model's self-hosted endpoints, read from the live table."""
        return frozenset(
            str(adapter.config.endpoint_id)
            for adapter in self._local_adapters()
            if getattr(adapter.config, "endpoint_id", None)
        )

    def _cloud_scope(self) -> frozenset[str]:
        """Return the model's other endpoints, read from the live table."""
        return self._route_endpoints() - self._local_scope()

    def _queued_local(self) -> frozenset[str]:
        """Return the model's self-hosted endpoints whose engine queue this router registered."""
        return frozenset(
            endpoint_id_for_adapter(adapter)
            for adapter in self._local_adapters()
            if getattr(adapter.config, "base_url", None) in self._queued
        )

    def _require_attached(self) -> tuple[FixedRouter, LocalBackend, FixedCloudBackend]:
        if self._fixed is None or self._local is None or self._cloud is None:
            raise GreedyRoutingError("router: greedy was used before its route table was attached")
        return self._fixed, self._local, self._cloud

    # ------------------------------------------------------------------
    # Engine queues and lifecycle
    # ------------------------------------------------------------------

    def _register(self) -> None:
        """Register the model's local engines with the limiter, or clear them.

        A request is sent only to the engines the last registration that
        succeeded named (``_queued``). One a route edit brought in and that could
        not be registered has no queue counting what it is sent, so a deadline
        would mean nothing there: it gets nothing until a registration names it.

        Raises:
            EngineQueueConflict: Another owner registered one of the engines
                with a different limit.
            EngineQueueUnavailable: Several worker processes serve this
                deployment.
        """
        if self._model_id is None:
            return
        limiter = get_upstream_limiter()
        limit = self._params.engine_queue_limit
        engines = {
            adapter.config.base_url: limit
            for adapter in self._local_adapters()
            if getattr(adapter.config, "base_url", None)
        }
        if not engines:
            limiter.clear_engine_queues(self._model_id, token=self._registration)
            self._registration = None
            self._queued = frozenset()
            return
        self._registration = limiter.set_engine_queues(self._model_id, engines)
        self._queued = frozenset(engines)

    async def start(self) -> bool:
        """Register this model's engine queues; True when this call did it."""
        if self._started:
            return False
        # Registered first, so a start that raises leaves the router unstarted.
        self._register()
        self._started = True
        self._stopped = False
        return True

    async def stop(self) -> bool:
        """Clear this router's engine queues; True when it had any to clear or was started.

        Only this router's own registration is cleared: a router built to replace
        it for the same model, and started first, keeps the queues it registered.
        A router that registered on its first request without being started
        (:meth:`_ensure_registered`) is cleared all the same. A stopped router
        registers nothing again until it is started.
        """
        had_work = self._started or self._registration is not None
        self._started = False
        self._stopped = True
        if self._model_id is not None and self._registration is not None:
            get_upstream_limiter().clear_engine_queues(self._model_id, token=self._registration)
        self._registration = None
        return had_work

    def refresh_route_table(self) -> None:
        """Follow a route edit: the pools re-derive their range, and the queues follow.

        Never raises: the registry refreshes every router in one pass, and one
        model's queues must not stop the others from following the edit. When
        the edit brought in an engine another model registers with a different
        limit, the registration this router had stays as it was and the conflict
        is logged; requests then go to none of the engines the edit brought in
        (see :meth:`_register`).
        """
        for pool in (self._local, self._cloud):
            refresh = getattr(pool, "refresh_route_table", None)
            if callable(refresh):
                refresh()
        if not (self._started or self._registration is not None):
            return
        try:
            self._register()
        except (EngineQueueConflict, EngineQueueUnavailable) as exc:
            logger.error(
                "greedy_engine_queues_not_updated",
                extra={
                    "event": "greedy_engine_queues_not_updated",
                    "model": self._model_id,
                    "error": str(exc),
                },
            )

    def _ensure_registered(self) -> None:
        """Register on the first request of a router that was never started.

        Bootstrap and the admin switch start every managed router, so this is
        the backstop for a caller that skipped it: without a queue, a deadline
        means nothing and every request would go local. A stopped router does
        not register again.
        """
        if self._started or self._stopped or self._registration is not None:
            return
        logger.warning(
            "greedy_router_not_started",
            extra={"event": "greedy_router_not_started", "model": self._model_id},
        )
        self._register()

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def _plan(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        routing_options: RoutingRequestOptions | None,
        params: dict[str, Any],
    ) -> _Plan:
        """Decide one request's steps and its deadline at the local engines.

        Each side's range is the one its pool enforces when it dispatches, so the
        plan never names an endpoint the pool would refuse, narrowed by the
        caller's range and by the request: a self-hosted route needs an engine
        queue this router registered, and a route on either side whose
        configured context window cannot fit the estimated prompt is passed over,
        as a cloud route is for a prompt over ``cloud_max_input_tokens``.
        """
        fixed, local_pool, cloud_pool = self._require_attached()
        arrival = time.monotonic()
        prompt_tokens = estimate_prefill_tokens(
            messages,
            tools=params.get("tools"),
            response_format=params.get("response_format"),
        )
        imposed = routing_options.endpoint_scope if routing_options is not None else None
        local = self._in_range(local_pool, imposed, model_id) & self._queued_local()
        cloud = self._in_range(cloud_pool, imposed, model_id)
        local, too_long_local = self._fitting(model_id, local, prompt_tokens)
        cloud, too_long_cloud = self._fitting(model_id, cloud, prompt_tokens)
        cap = self._params.cloud_max_input_tokens
        capped = bool(cloud) and cap is not None and prompt_tokens > cap
        if capped:
            cloud = frozenset()

        deadline: float | None = None
        if local and cloud and self._cloud_takes(fixed, model_id, cloud, routing_options):
            deadline = self._deadline(prompt_tokens, arrival)
        base = routing_options if routing_options is not None else RoutingRequestOptions()
        local_options = (
            replace(base, endpoint_scope=local, engine_queue_deadline=deadline) if local else None
        )
        return _Plan(
            local=local_options,
            local_retry=(
                replace(local_options, engine_queue_deadline=None) if local_options else None
            ),
            cloud=(
                replace(base, endpoint_scope=cloud, engine_queue_deadline=None) if cloud else None
            ),
            spillover=deadline is not None and deadline <= arrival,
            capped=capped,
            too_long=too_long_local | too_long_cloud,
            prompt_tokens=prompt_tokens,
        )

    @staticmethod
    def _in_range(
        pool: LocalBackend | FixedCloudBackend,
        imposed: frozenset[str] | None,
        model_id: str,
    ) -> frozenset[str]:
        """Return a pool's endpoints for ``model_id`` inside the caller's range.

        The pool's own range is the one it enforces (``dispatch_scope``, derived
        again whenever the route table is refreshed), read as the pool reads it.
        Both sides are expanded to canonical endpoints first, as the hybrid layer
        does, because the caller's range may name provider labels. An empty
        grant stays empty.
        """
        declared = pool.dispatch_scope(model_id)
        if not declared:
            return frozenset()
        own = pool.scope_endpoints(declared, model_id)
        if imposed is None:
            return own
        return own & pool.scope_endpoints(imposed, model_id)

    def _fitting(
        self, model_id: str, endpoints: frozenset[str], prompt_tokens: int
    ) -> tuple[frozenset[str], frozenset[str]]:
        """Split endpoints into those whose configured window fits the prompt, and the rest."""
        route = self._fixed.routes.get(model_id) if self._fixed is not None else None
        adapters = {
            endpoint_id_for_adapter(adapter): adapter
            for adapter, _weight in (route.adapters if route is not None else ())
        }
        too_long = frozenset(
            endpoint
            for endpoint in endpoints
            if endpoint in adapters and not _fits_window(adapters[endpoint], prompt_tokens)
        )
        return endpoints - too_long, too_long

    def _deadline(self, prompt_tokens: int, arrival: float) -> float | None:
        """Return how long a request may wait for a local place, by this model's mode."""
        if self._params.cutoff is not None:
            return cutoff_deadline(self._params.cutoff, prompt_tokens, arrival)
        return arrival + self._params.max_wait_seconds

    @staticmethod
    def _cloud_takes(
        fixed: FixedRouter,
        model_id: str,
        cloud: frozenset[str],
        routing_options: RoutingRequestOptions | None,
    ) -> bool:
        """Return whether a cloud route could serve this request right now.

        Asked of the shared router's own candidate query (``eligible_adapters``:
        weight and circuit) over a range the plan already fitted to the prompt,
        narrowed by the request's media and the caller's keys. A request no cloud
        route can take waits for a local place with no deadline: leaving the
        queue would only fail it.
        """
        modalities = routing_options.required_modalities if routing_options is not None else None
        try:
            candidates = fixed.eligible_adapters(model_id, endpoint_scope=cloud)
        except Exception:  # the route can vanish between the plan and the read
            return False
        return any(
            adapter_supports_modalities(adapter, modalities) and _serves_caller(adapter)
            for adapter, _weight in candidates
        )

    def _cloud_step(self, ledger: AttemptLedger, plan: _Plan, *, unsent: bool) -> _Step:
        """Return the cloud step of a request the local step did not serve.

        Its reason is ``last_resort`` when no local engine could take the request
        at all, the queue's when an engine left it unsent (``unsent``, see
        :meth:`_left_engine_unsent`), and none after a local failure alone, which
        goes to the cloud as an ordinary fallback.
        """
        if plan.local is None:
            reason: str | None = OFFLOAD_LAST_RESORT
        elif unsent:
            reason = OFFLOAD_SPILLOVER if plan.spillover else OFFLOAD_QUEUE_WAIT
        elif ledger.refusals and not ledger.errors:
            reason = OFFLOAD_LAST_RESORT
        else:
            reason = None
        return _Step(
            after_local=plan.local is not None,
            earlier=tuple(ledger.attempts),
            offload=reason,
        )

    @staticmethod
    def _left_engine_unsent(ledger: AttemptLedger) -> bool:
        """Return whether the local step left an engine that could still take the request.

        Either every engine it tried left it unsent at the deadline, or one did
        while another failed: the local pool then raises the failure, and names
        the engines it passed over (``engines_left_unsent``).
        """
        return bool(ledger.unsent) or any(engines_left_unsent(exc) for exc in ledger.errors)

    @staticmethod
    def _retry_step(ledger: AttemptLedger, cloud: _Step, recorded: int) -> _Step:
        """Return the local step after the cloud step did not serve.

        A request the cloud was sent stays offloaded whichever route answers it,
        as the shared router's offload does, and its cloud route is named as
        ``offload_endpoint_id``. One the cloud refused, or that waited for an
        outbound place in this gateway and never left it, was never sent there
        (:func:`_sent_to`). ``recorded`` is how many attempts the ledger held
        before the cloud step.
        """
        sent_to: str | None = None
        for record, exc in zip(ledger.attempts[recorded:], ledger.errors[recorded:], strict=False):
            sent_to = _sent_to(exc, record) or sent_to
        return _Step(
            after_local=True,
            earlier=tuple(ledger.attempts),
            offload=cloud.offload if sent_to is not None else None,
            offload_endpoint_id=sent_to,
        )

    @staticmethod
    def _plan_ends(ledger: AttemptLedger, model_id: str, errors_before: int) -> bool:
        """Return whether a step's new failure ends the plan here.

        The shared router's bound on a malformed request
        (``AttemptLedger.refusals_end_plan``): once enough steps refused the
        request itself, it is sent to no further pool. Asked only after a step
        added a failure, as the hybrid router asks it.
        """
        return len(ledger.errors) > errors_before and ledger.refusals_end_plan(model_id)

    def _log_spill(self, model_id: str, reason: str | None) -> None:
        if reason is None:
            return
        logger.info(
            "route_offload",
            extra={
                "event": "route_offload",
                "model_id": model_id,
                "reason": reason,
                "router": "greedy",
            },
        )

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
        """Route one non-streaming request through at most three steps."""
        fixed, local_pool, cloud_pool = self._require_attached()
        if _caller_owns_dispatch(routing_options):
            return await fixed.chat_completion(
                model_id, messages, routing_options=routing_options, **params
            )
        self._ensure_registered()
        plan = self._plan(model_id, messages, routing_options, params)
        ledger = AttemptLedger()

        async def run(
            pool: Any, name: str, options: RoutingRequestOptions, *, may_leave: bool = True
        ) -> dict[str, Any] | None:
            return await chat_attempt(
                PoolAttempt(
                    backend=pool, backend_name=name, endpoint_id=None, routing_options=options
                ),
                model_id=model_id,
                messages=messages,
                params=params,
                ledger=ledger,
                left_unsent=_left_unsent if may_leave else None,
            )

        step = _Step()
        if plan.local is not None:
            response = await run(local_pool, _LOCAL, plan.local)
            if response is not None:
                return step.served(response, _LOCAL)
        unsent = self._left_engine_unsent(ledger)
        ended = self._plan_ends(ledger, model_id, 0)
        if plan.cloud is not None and not ended:
            step = self._cloud_step(ledger, plan, unsent=unsent)
            self._log_spill(model_id, step.offload)
            recorded = len(ledger.attempts)
            response = await run(cloud_pool, _CLOUD, plan.cloud)
            if response is not None:
                return step.served(response, _CLOUD)
            step = self._retry_step(ledger, step, recorded)
            ended = self._plan_ends(ledger, model_id, recorded)
        if unsent and plan.local_retry is not None and not ended:
            response = await run(local_pool, _LOCAL, plan.local_retry, may_leave=False)
            if response is not None:
                return step.served(response, _LOCAL)
        ledger.raise_after(
            lambda: self._nothing_in_range(model_id, routing_options, plan), mark=step.mark
        )

    def _nothing_in_range(
        self,
        model_id: str,
        routing_options: RoutingRequestOptions | None,
        plan: _Plan,
    ) -> AllCircuitsOpenError:
        """Build the error for a request neither pool could take at all.

        The shared router's own error for a dispatch range with no route in it,
        so the serving layer answers it the same way.
        """
        detail = f"No route for model {model_id} can take this request"
        scope = routing_options.endpoint_scope if routing_options is not None else None
        if scope is not None:
            detail += f" inside the dispatch scope: {sorted(scope)}"
        if plan.too_long:
            detail += (
                f"; its estimated prompt of {plan.prompt_tokens} tokens is longer than "
                f"the context window of {sorted(plan.too_long)}"
            )
        if plan.capped:
            detail += (
                "; its estimated prompt is over cloud_max_input_tokens "
                f"({self._params.cloud_max_input_tokens}), so no cloud route takes it"
            )
        return AllCircuitsOpenError(detail)

    def stream_chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> AsyncIterator[Any]:
        """Route one streaming request through at most three steps.

        Nothing runs until the first read, as in the shared router: the serving
        layer turns a routing error raised while it reads the stream into its
        status. A step after the first opens with a routing frame that marks a
        new attempt, so the request log names the route that answered and why
        the request left its engine.
        """
        return self._stream(model_id, messages, routing_options, params)

    async def _stream(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        routing_options: RoutingRequestOptions | None,
        params: dict[str, Any],
    ) -> AsyncIterator[Any]:
        fixed, local_pool, cloud_pool = self._require_attached()
        if _caller_owns_dispatch(routing_options):
            async with aclosing(
                fixed.stream_chat_completion(
                    model_id, messages, routing_options=routing_options, **params
                )
            ) as chunks:
                async for chunk in chunks:
                    yield chunk
            return
        self._ensure_registered()
        plan = self._plan(model_id, messages, routing_options, params)
        ledger = AttemptLedger()

        def run(
            pool: Any,
            name: str,
            options: RoutingRequestOptions,
            outcome: StreamOutcome,
            step: _Step,
            *,
            may_leave: bool = True,
        ) -> Any:
            # Closed with this generator, so a client that leaves closes the
            # pool's stream at once.
            return aclosing(
                stream_attempt(
                    PoolAttempt(
                        backend=pool, backend_name=name, endpoint_id=None, routing_options=options
                    ),
                    model_id=model_id,
                    messages=messages,
                    params=params,
                    ledger=ledger,
                    outcome=outcome,
                    lead_frame=step.lead_frame,
                    stamp=step.stamp,
                    left_unsent=_left_unsent if may_leave else None,
                )
            )

        step = _Step()
        if plan.local is not None:
            outcome = StreamOutcome()
            async with run(local_pool, _LOCAL, plan.local, outcome, step) as chunks:
                async for chunk in chunks:
                    yield chunk
            if outcome.served:
                return
        unsent = self._left_engine_unsent(ledger)
        ended = self._plan_ends(ledger, model_id, 0)
        if plan.cloud is not None and not ended:
            step = self._cloud_step(ledger, plan, unsent=unsent)
            self._log_spill(model_id, step.offload)
            recorded = len(ledger.attempts)
            outcome = StreamOutcome()
            async with run(cloud_pool, _CLOUD, plan.cloud, outcome, step) as chunks:
                async for chunk in chunks:
                    yield chunk
            if outcome.served:
                return
            step = self._retry_step(ledger, step, recorded)
            ended = self._plan_ends(ledger, model_id, recorded)
        if unsent and plan.local_retry is not None and not ended:
            outcome = StreamOutcome()
            async with run(
                local_pool, _LOCAL, plan.local_retry, outcome, step, may_leave=False
            ) as chunks:
                async for chunk in chunks:
                    yield chunk
            if outcome.served:
                return
        ledger.raise_after(
            lambda: self._nothing_in_range(model_id, routing_options, plan), mark=step.mark
        )

    # ------------------------------------------------------------------
    # Feedback and status
    # ------------------------------------------------------------------

    def record_observation(self, obs: RoutingObservation) -> None:
        """Hand feedback to the shared router, which both pools wrap and which ran it."""
        if self._fixed is not None:
            self._fixed.record_observation(obs)

    def get_provider_status(self) -> dict[str, dict[str, Any]]:
        """Return the shared router's endpoint health and circuit state."""
        return self._fixed.get_provider_status() if self._fixed is not None else {}

    def canonical_id(self, model_id: str) -> str:
        """Resolve ``model_id`` through the shared router."""
        return self._fixed.canonical_id(model_id) if self._fixed is not None else model_id
