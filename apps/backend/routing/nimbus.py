"""The Nimbus router: a model's local route goes to the admission process beside its engine.

``router: nimbus`` (``routing.strategies.nimbus``) is for a model whose
self-hosted routes each point at a Nimbus router in admission-only mode
(``admission: "nimbus"`` on the route) and which has cloud routes beside them.
Whether a request stays on the engine is that process's decision, not this
gateway's: it holds the engine's queue, counts the prompt's exact tokens, and
follows the engine's cache. This router sends it each request with what only the
gateway knows -- how soon the cloud would answer instead, and how long the
request has been here (``serving.adapters.engine_admission``) -- and carries out
the answer:

- **A response**: the request stays on the engine.
- **A decline** (``EngineDeclined``): nothing reached the engine, and the request
  goes to the model's cloud routes, once.
- **A failure before any output**: the request goes to the cloud once, as an
  ordinary fallback.

Each request is at most two steps, local then cloud. The cloud failing is the
request's answer: the process declined because the engine cannot take the
request, so there is no local step to return to. A request no cloud route can
take is sent with no cloud time at all, and the process then keeps it unless the
engine truly cannot serve it; declined all the same, it ends as "no route can
take this request".

Some requests never go to the process (:func:`unsupported_locally`): what its
route cannot serve as asked goes to the cloud at once.

This gateway keeps no queue for these engines. The router registers none, and a
route whose address has an engine queue registered by another model is left out:
a request that waited here first would reach the process late, with its arrival
and the engine's line both misread. Both pools wrap the shared ``FixedRouter``,
scoped to the model's declared routes and to the rest, so selection, health,
affinity, execution and accounting are its own, and each step runs through
:mod:`routing.pool_attempts`. The design is
``docs/agents/specs/2026-11-26-nimbus-admission-design.zh.md``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from contextlib import aclosing
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, NoReturn

from routing.backends import FixedCloudBackend, LocalBackend
from routing.endpoints import endpoint_id_for_adapter

# The step bookkeeping, the caller-owned check and the range helpers are the
# greedy router's own, used as they are so the two routers cannot drift apart.
from routing.greedy import GreedyRouter, _caller_owns_dispatch, _fits_window, _Step
from routing.offload import OFFLOAD_LAST_RESORT
from routing.pool_attempts import (
    AttemptLedger,
    PoolAttempt,
    StreamOutcome,
    chat_attempt,
    stream_attempt,
)
from routing.prefill_load import estimate_prefill_tokens
from routing.protocols import RoutingRequestOptions
from routing.routers import AllCircuitsOpenError
from serving.adapters.engine_admission import AdmissionTerms, admission_context
from serving.adapters.upstream_limiter import (
    EngineDeclined,
    engine_address,
    get_upstream_limiter,
    is_local_endpoint,
)
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Collection

    from routing.routers import FixedRouter, RoutingObservation
    from routing.strategies.greedy import CloudTtft
    from routing.strategies.nimbus import NimbusParams

logger = get_logger(__name__)

__all__ = [
    "OFFLOAD_UNSUPPORTED_LOCAL",
    "AdmissionSurfacePlan",
    "NimbusRouter",
    "NimbusRoutingError",
    "cloud_ttft_ms",
    "unsupported_locally",
]

#: ``_routing["offload"]`` for a request that was never sent to the admission
#: process because its route cannot serve it as asked. A request the process
#: declined carries the process's own reason, such as ``declined_at_cutoff``, and
#: one no local route could take at all the shared ``last_resort``.
OFFLOAD_UNSUPPORTED_LOCAL = "unsupported_local"

#: The value a route declares (``ModelConfig.admission``).
_ADMISSION = "nimbus"

_LOCAL = "local"
_CLOUD = "cloud"


class NimbusRoutingError(RuntimeError):
    """The Nimbus router cannot route for the model it was given.

    A composition or configuration problem -- no route table yet, or routes that
    break what the admission process relies on -- never an upstream failure.
    """


def cloud_ttft_ms(cloud_ttft: CloudTtft, prompt_tokens: int) -> float:
    """Return the cloud's first-token time for a prompt, in milliseconds.

    The line the greedy router's Cutoff uses (``routing.greedy.cutoff_deadline``):
    a fixed part plus a time per prompt token, the prompt counted whole whatever
    is cached.
    """
    return cloud_ttft.fixed_ms + cloud_ttft.per_input_token_ms * prompt_tokens


def unsupported_locally(params: Mapping[str, Any]) -> str | None:
    """Return what in a request the admission process's route cannot serve as asked, or None.

    The process sends the engine token ids and the sampling parameters alone and
    reads tool calls from the reply itself, so it refuses a call it would have to
    force or keep single and the legacy function fields, and cannot constrain a
    reply's format. Sent anyway, each would come back as a 400 and spend one of
    the request's two refusals before the cloud was asked; this names them first.
    The list is the process's own (its ``unsupported``), restated here: one it
    grows is still served, by that refusal and the cloud.
    """
    form = params.get("response_format")
    if form is not None and not (isinstance(form, Mapping) and form.get("type") == "text"):
        return "response_format"
    if params.get("tool_choice") not in (None, "auto", "none"):
        return "tool_choice"
    if params.get("parallel_tool_calls") is False:
        return "parallel_tool_calls"
    if "functions" in params or "function_call" in params:
        return "functions"
    return None


def _declined(exc: BaseException) -> bool:
    """Whether an attempt ended in the admission process's decline, sending the engine nothing."""
    return isinstance(exc, EngineDeclined)


def _asks_for_tools(params: Mapping[str, Any]) -> bool:
    return bool(params.get("tools")) and params.get("tool_choice") != "none"


@dataclass(frozen=True, slots=True)
class _Plan:
    """One request's terms, decided when it arrives.

    ``local`` is the first step's options, or None when the request is not sent
    to an admission process: no declared route can take it, or its route cannot
    serve it as asked (``unsupported`` then says what). ``terms`` is what the
    process is told. The cloud step works its range out again just before it
    runs, as the greedy router's does. ``capped`` and ``too_long`` say why the
    ranges were narrower than the pools', for the error a request no side can
    take is told.
    """

    base: RoutingRequestOptions
    imposed: frozenset[str] | None
    prompt_tokens: int
    local: RoutingRequestOptions | None
    terms: AdmissionTerms
    unsupported: str | None = None
    capped: bool = False
    too_long: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class AdmissionSurfacePlan:
    """Where a request goes, for a surface that dispatches its own adapter.

    ``/v1/messages`` forwards an Anthropic body the router has no method for,
    so it picks the adapter itself; this is what
    :meth:`NimbusRouter.surface_plan` tells it, by the router's own rules.

    Attributes:
        local: The declared endpoints the request may be sent to first, empty
            when it is not sent to an admission process at all.
        cloud: The endpoints it may go to instead.
        terms: What the process is told about the request.
        unsupported: What in the request the process's route cannot serve as
            asked, or None.
        capped: Whether the cloud's input limit left the request no cloud route.
        too_long: The routes left out because their configured context window
            cannot fit the prompt.
    """

    local: frozenset[str]
    cloud: frozenset[str]
    terms: AdmissionTerms
    unsupported: str | None = None
    capped: bool = False
    too_long: frozenset[str] = frozenset()

    @property
    def offload(self) -> str:
        """Return ``_routing["offload"]`` for a request that is not sent to the process."""
        return OFFLOAD_UNSUPPORTED_LOCAL if self.unsupported is not None else OFFLOAD_LAST_RESORT


class NimbusRouter:
    """Send a model's requests to the admission process beside its engine, then the cloud.

    Built by the strategy registry (``build_router``) with its parameters, then
    handed the shared ``FixedRouter`` by ``ModelRouterRegistry`` through
    :meth:`attach_route_table`, scoped to its one model. A ``ManagedRouter`` with
    nothing of its own to start: it registers no engine queue.

    Args:
        params: The model's validated ``router_params``.
        health_registry: Accepted for the registry's dependency injection; the
            shared router carries the endpoint health this router's pools use.
    """

    def __init__(self, *, params: NimbusParams, health_registry: Any = None) -> None:
        del health_registry
        self._params = params
        self._fixed: FixedRouter | None = None
        self._model_id: str | None = None
        self._local: LocalBackend | None = None
        self._cloud: FixedCloudBackend | None = None
        self._started = False

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    @property
    def params(self) -> NimbusParams:
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

        Builds the two pools over it: the model's routes that declare the
        admission process and the rest, each read from the live route table on
        every request.

        Raises:
            NimbusRoutingError: ``model_scope`` does not name exactly one model,
                or the model's routes break what the process relies on
                (:meth:`route_problems`).
        """
        scope = frozenset(model_scope or ())
        if len(scope) != 1:
            raise NimbusRoutingError(
                f"router: nimbus serves exactly one model and was given {sorted(scope)}"
            )
        (model_id,) = scope
        self._fixed = route_table
        self._model_id = model_id
        problems = self.route_problems()
        if problems:
            raise NimbusRoutingError(
                f"router: nimbus cannot serve model {model_id!r}: {problems[0]}"
            )
        self._local = LocalBackend(
            route_table, endpoint_scope=self._local_scope, model_scope={model_id}
        )
        self._cloud = FixedCloudBackend(
            route_table, endpoint_scope=self._cloud_scope, model_scope={model_id}
        )

    def _adapters(self) -> list[Any]:
        """Return every adapter on the model's route now, weight 0 included."""
        route = self._fixed.routes.get(self._model_id) if self._fixed is not None else None
        if route is None or not route.adapters:
            return []
        return [adapter for adapter, _weight in route.adapters]

    @staticmethod
    def _declares(adapter: Any) -> bool:
        return getattr(adapter.config, "admission", None) == _ADMISSION

    @staticmethod
    def _self_hosted(adapter: Any) -> bool:
        return is_local_endpoint(
            getattr(adapter.config, "base_url", None),
            deployment_type=getattr(adapter.config, "deployment_type", "auto"),
        )

    def _declared_adapters(self) -> list[Any]:
        """Return the model's routes that declare the admission process."""
        return [adapter for adapter in self._adapters() if self._declares(adapter)]

    def route_problems(self) -> list[str]:
        """Return why the model's routes cannot be served by this router, empty when they can.

        The admission process must be the engine's only sender and its only
        queue, and this gateway can check that as far as its own routes go:

        - at least one route declares the process, and at least one does not;
        - a route that declares it is self-hosted by the gateway's one
          classification, so no outbound limit queues its requests here first;
        - its address has no engine queue registered, by any model;
        - no route of the model reaches the same address without declaring it;
        - every other route is a cloud route: a self-hosted engine with no
          process in front has no queue at all under this router;
        - it declares text input only, which is all the process serves.

        A route to the engine itself, past the process, is not something the
        gateway can see: its configuration names the process's address only.
        """
        model_id = self._model_id
        declared = self._declared_adapters()
        others = [adapter for adapter in self._adapters() if not self._declares(adapter)]
        problems: list[str] = []
        if not declared:
            problems.append(f'no route of model {model_id!r} declares admission: "nimbus"')
        if not others:
            problems.append(f"model {model_id!r} needs a cloud route beside its declared ones")
        limiter = get_upstream_limiter()
        addresses = set()
        for adapter in declared:
            endpoint = endpoint_id_for_adapter(adapter)
            base_url = getattr(adapter.config, "base_url", None)
            addresses.add(engine_address(base_url))
            if not self._self_hosted(adapter):
                problems.append(
                    f"route {endpoint} declares admission but is not self-hosted: "
                    "set deployment_type: self_hosted"
                )
            if limiter.engine_queue_limit(base_url) is not None:
                problems.append(
                    f"route {endpoint} declares admission and its address also has an engine "
                    "queue registered: one engine has one queue"
                )
            modalities = set(getattr(adapter.config, "input_modalities", None) or ["text"])
            if modalities - {"text"}:
                problems.append(
                    f"route {endpoint} declares admission and input other than text "
                    f"({sorted(modalities - {'text'})}), which the process does not serve"
                )
        for adapter in others:
            endpoint = endpoint_id_for_adapter(adapter)
            if engine_address(getattr(adapter.config, "base_url", None)) in addresses:
                problems.append(
                    f"route {endpoint} reaches a declared route's address without declaring "
                    "admission itself"
                )
            elif self._self_hosted(adapter):
                problems.append(
                    f"route {endpoint} is self-hosted and declares no admission: under "
                    "router: nimbus every self-hosted route goes through the process"
                )
        return problems

    def _route_endpoints(self) -> frozenset[str]:
        return frozenset(endpoint_id_for_adapter(adapter) for adapter in self._adapters())

    def _local_scope(self) -> frozenset[str]:
        """Return the model's declared endpoints, read from the live table.

        One whose address has gained an engine queue since is left out: a request
        would wait in that line before the process ever saw it.
        """
        limiter = get_upstream_limiter()
        return frozenset(
            endpoint_id_for_adapter(adapter)
            for adapter in self._declared_adapters()
            if limiter.engine_queue_limit(getattr(adapter.config, "base_url", None)) is None
        )

    def _cloud_scope(self) -> frozenset[str]:
        """Return the model's cloud endpoints, read from the live table.

        Neither a declared route nor any other self-hosted one: a route edited
        into a bare engine since this router was attached gets none of its
        requests, since nothing would queue for it.
        """
        return frozenset(
            endpoint_id_for_adapter(adapter)
            for adapter in self._adapters()
            if not self._declares(adapter) and not self._self_hosted(adapter)
        )

    def _require_attached(self) -> tuple[FixedRouter, LocalBackend, FixedCloudBackend]:
        if self._fixed is None or self._local is None or self._cloud is None:
            raise NimbusRoutingError("router: nimbus was used before its route table was attached")
        return self._fixed, self._local, self._cloud

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        """Start the router; True when this call did it. It holds nothing to start."""
        if self._started:
            return False
        self._started = True
        return True

    async def stop(self) -> bool:
        """Stop the router; True when it was started."""
        was_started = self._started
        self._started = False
        return was_started

    def refresh_route_table(self) -> None:
        """Follow a route edit: the pools re-derive their range.

        Never raises, as the registry refreshes every router in one pass. Routes
        an edit left breaking what the process relies on are logged; the ranges
        (:meth:`_local_scope`, :meth:`_cloud_scope`) already leave out the ones
        that would bypass or double the engine's queue.
        """
        for pool in (self._local, self._cloud):
            refresh = getattr(pool, "refresh_route_table", None)
            if callable(refresh):
                refresh()
        if self._fixed is None:
            return
        problems = self.route_problems()
        if problems:
            logger.error(
                "nimbus_routes_invalid",
                extra={
                    "event": "nimbus_routes_invalid",
                    "model": self._model_id,
                    "problems": problems,
                },
            )

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
        """Decide one request's steps and what the admission process is told."""
        prompt_tokens = estimate_prefill_tokens(
            messages,
            tools=params.get("tools"),
            response_format=params.get("response_format"),
        )
        imposed = routing_options.endpoint_scope if routing_options is not None else None
        base = routing_options if routing_options is not None else RoutingRequestOptions()
        sides = self.surface_plan(
            model_id,
            prompt_tokens,
            asks=params,
            imposed=imposed,
            required_modalities=base.required_modalities,
            session_id=params.get("session_id"),
            request_id=params.get("request_id"),
        )
        return _Plan(
            base=base,
            imposed=imposed,
            prompt_tokens=prompt_tokens,
            # One process is asked, once: it answers for its engine, and a second
            # would be asked later with less of the budget left.
            local=(
                replace(base, endpoint_scope=sides.local, allow_fallback=False)
                if sides.local
                else None
            ),
            terms=sides.terms,
            unsupported=sides.unsupported,
            capped=sides.capped,
            too_long=sides.too_long,
        )

    def surface_plan(
        self,
        model_id: str,
        prompt_tokens: int,
        *,
        asks: Mapping[str, Any] | None = None,
        imposed: frozenset[str] | None = None,
        required_modalities: frozenset[str] = frozenset(),
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> AdmissionSurfacePlan:
        """Plan a request for a surface that dispatches its own adapter.

        The ranges, the check of what the process's route cannot serve and the
        terms of this router's own requests, so a surface that cannot hand this
        router its request still follows the same rule. ``asks`` is what the
        request asks in the chat parameters' terms (``tools``, ``tool_choice``,
        ``parallel_tool_calls``, ``response_format``), ``imposed`` the caller's
        range, and ``required_modalities`` the media it carries, which a cloud
        route must accept for the process to be told a cloud time at all.
        """
        fixed, local_pool, _cloud_pool = self._require_attached()
        asks = asks or {}
        unsupported = unsupported_locally(asks)
        local: frozenset[str] = frozenset()
        too_long_local: frozenset[str] = frozenset()
        if unsupported is None:
            in_range = GreedyRouter._in_range(local_pool, imposed, model_id)
            if in_range and _asks_for_tools(asks):
                reads_tools = self._reading_tools(in_range)
                if not reads_tools:
                    unsupported = "tools"
                in_range = reads_tools
            local, too_long_local = self._fitting(model_id, in_range, prompt_tokens)
        cloud, too_long_cloud, capped = self._cloud_range(model_id, imposed, prompt_tokens)
        media = (
            RoutingRequestOptions(required_modalities=required_modalities)
            if required_modalities
            else None
        )
        takes = bool(cloud) and GreedyRouter._cloud_takes(fixed, model_id, cloud, media)
        return AdmissionSurfacePlan(
            local=local,
            cloud=cloud,
            terms=AdmissionTerms(
                remote_ttft_ms=(
                    cloud_ttft_ms(self._params.cloud_ttft, prompt_tokens) if takes else math.inf
                ),
                session_id=session_id,
                request_id=request_id,
            ),
            unsupported=unsupported,
            capped=capped,
            too_long=too_long_local | too_long_cloud,
        )

    def _reading_tools(self, endpoints: frozenset[str]) -> frozenset[str]:
        """Return the endpoints among ``endpoints`` whose route takes tools."""
        return frozenset(
            endpoint_id_for_adapter(adapter)
            for adapter in self._declared_adapters()
            if endpoint_id_for_adapter(adapter) in endpoints
            and getattr(adapter.config, "supports_tools", False)
        )

    def _fitting(
        self, model_id: str, endpoints: frozenset[str], prompt_tokens: int
    ) -> tuple[frozenset[str], frozenset[str]]:
        """Split endpoints into those whose configured window fits the prompt, and the rest."""
        adapters = {endpoint_id_for_adapter(adapter): adapter for adapter in self._adapters()}
        too_long = frozenset(
            endpoint
            for endpoint in endpoints
            if endpoint in adapters and not _fits_window(adapters[endpoint], prompt_tokens)
        )
        return endpoints - too_long, too_long

    def _cloud_range(
        self, model_id: str, imposed: frozenset[str] | None, prompt_tokens: int
    ) -> tuple[frozenset[str], frozenset[str], bool]:
        """Return the cloud endpoints a step may use now, those too small, and whether the cap cut it."""
        _fixed, _local_pool, cloud_pool = self._require_attached()
        cloud = GreedyRouter._in_range(cloud_pool, imposed, model_id) & self._cloud_scope()
        cloud, too_long = self._fitting(model_id, cloud, prompt_tokens)
        cap = self._params.cloud_max_input_tokens
        capped = bool(cloud) and cap is not None and prompt_tokens > cap
        return (frozenset() if capped else cloud), too_long, capped

    def _cloud_options(self, model_id: str, plan: _Plan) -> RoutingRequestOptions | None:
        """Return the cloud step's options with its range worked out now, or None for none."""
        cloud, _too_long, _capped = self._cloud_range(model_id, plan.imposed, plan.prompt_tokens)
        return replace(plan.base, endpoint_scope=cloud) if cloud else None

    @staticmethod
    def _decline(ledger: AttemptLedger) -> EngineDeclined | None:
        """Return the process's decline of this request, when the local step ended in one."""
        return next((exc for exc in ledger.unsent if isinstance(exc, EngineDeclined)), None)

    def _cloud_step(self, ledger: AttemptLedger, plan: _Plan) -> _Step:
        """Return the cloud step of a request the local step did not serve.

        Its reason is the process's own when it declined the request,
        ``unsupported_local`` when the request was never sent to it for what it
        asks, ``last_resort`` when no declared route could take it at all, and
        none after a local failure alone, which goes to the cloud as an ordinary
        fallback.
        """
        declined = self._decline(ledger)
        if plan.local is None:
            reason: str | None = (
                OFFLOAD_UNSUPPORTED_LOCAL if plan.unsupported is not None else OFFLOAD_LAST_RESORT
            )
        elif declined is not None:
            reason = declined.decision
        elif ledger.refusals and not ledger.errors:
            reason = OFFLOAD_LAST_RESORT
        else:
            reason = None
        return _Step(
            after_local=plan.local is not None,
            earlier=tuple(ledger.attempts),
            offload=reason,
        )

    def _log_offload(self, model_id: str, plan: _Plan, step: _Step, ledger: AttemptLedger) -> None:
        if step.offload is None:
            return
        extra: dict[str, Any] = {
            "event": "route_offload",
            "model_id": model_id,
            "reason": step.offload,
            "router": "nimbus",
        }
        declined = self._decline(ledger)
        if declined is not None:
            extra["ttft_left_ms"] = declined.ttft_left_ms
            extra["engine_request_id"] = declined.engine_request_id
        elif plan.unsupported is not None:
            extra["unsupported"] = plan.unsupported
        logger.info("route_offload", extra=extra)

    def _finish(
        self,
        ledger: AttemptLedger,
        model_id: str,
        routing_options: RoutingRequestOptions | None,
        plan: _Plan,
        step: _Step,
    ) -> NoReturn:
        """Raise the error a request no step served is told.

        A failure is the caller's answer, as the ledger picks it. A request that
        only ever met a decline -- the process would not take it and no cloud
        route could -- is told that no route can take it, the shared router's own
        error for it, which the serving layer answers 503: the decline itself
        carries no status, and is not the caller's to read.
        """
        declined = self._decline(ledger)
        if declined is not None and not ledger.errors and not ledger.refusals:
            raise self._nothing_takes(model_id, routing_options, plan, declined) from declined
        ledger.raise_after(
            lambda: self._nothing_takes(model_id, routing_options, plan, None), mark=step.mark
        )

    @staticmethod
    def _chosen_terms(params: Mapping[str, Any]) -> AdmissionTerms:
        """Return what the process is told of a request whose caller chose the endpoint.

        A pin, a binding or a required target leaves this router no cloud step,
        so no cloud route takes the request over: it is told there is no cloud
        time, and keeps the request unless the engine cannot serve it at all.
        """
        return AdmissionTerms(
            remote_ttft_ms=math.inf,
            session_id=params.get("session_id"),
            request_id=params.get("request_id"),
        )

    @staticmethod
    def _declined_where_chosen(model_id: str, declined: EngineDeclined) -> AllCircuitsOpenError:
        """Build the error for a request declined at the endpoint its caller chose.

        The shared router's error for a request no route can take, as a request
        this router declined on both sides is told (:meth:`_nothing_takes`): the
        decline carries no status, and raised as it is the serving layer would
        answer it as an internal fault.
        """
        return AllCircuitsOpenError(
            f"No route for model {model_id} can take this request: its engine declined it "
            f"({declined.decision}) at the endpoint the caller chose"
        )

    def _nothing_takes(
        self,
        model_id: str,
        routing_options: RoutingRequestOptions | None,
        plan: _Plan,
        declined: EngineDeclined | None,
    ) -> AllCircuitsOpenError:
        """Build the error for a request neither side could take."""
        detail = f"No route for model {model_id} can take this request"
        scope = routing_options.endpoint_scope if routing_options is not None else None
        if scope is not None:
            detail += f" inside the dispatch scope: {sorted(scope)}"
        if declined is not None:
            detail += f"; its engine declined it ({declined.decision}) and no cloud route takes it"
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
        """Route one non-streaming request through at most two steps."""
        fixed, local_pool, cloud_pool = self._require_attached()
        if _caller_owns_dispatch(routing_options):
            try:
                with req_ctx.push(**admission_context(self._chosen_terms(params))):
                    return await fixed.chat_completion(
                        model_id, messages, routing_options=routing_options, **params
                    )
            except EngineDeclined as declined:
                raise self._declined_where_chosen(model_id, declined) from declined
        plan = self._plan(model_id, messages, routing_options, params)
        ledger = AttemptLedger()

        async def run(
            pool: Any, name: str, options: RoutingRequestOptions, *, may_decline: bool
        ) -> dict[str, Any] | None:
            return await chat_attempt(
                PoolAttempt(
                    backend=pool, backend_name=name, endpoint_id=None, routing_options=options
                ),
                model_id=model_id,
                messages=messages,
                params=params,
                ledger=ledger,
                left_unsent=_declined if may_decline else None,
            )

        step = _Step()
        if plan.local is not None:
            with req_ctx.push(**admission_context(plan.terms)):
                response = await run(local_pool, _LOCAL, plan.local, may_decline=True)
            if response is not None:
                return step.served(response, _LOCAL)
        ended = bool(ledger.errors) and ledger.refusals_end_plan(model_id)
        cloud = None if ended else self._cloud_options(model_id, plan)
        if cloud is not None:
            step = self._cloud_step(ledger, plan)
            self._log_offload(model_id, plan, step, ledger)
            response = await run(cloud_pool, _CLOUD, cloud, may_decline=False)
            if response is not None:
                return step.served(response, _CLOUD)
        else:
            step = _Step(after_local=plan.local is not None, earlier=tuple(ledger.attempts))
        self._finish(ledger, model_id, routing_options, plan, step)

    def stream_chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> AsyncIterator[Any]:
        """Route one streaming request through at most two steps.

        Nothing runs until the first read, as in the shared router. The cloud
        step opens with a routing frame that marks a new attempt, so the request
        log names the route that answered and why the request left its engine.
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
            try:
                with req_ctx.push(**admission_context(self._chosen_terms(params))):
                    async with aclosing(
                        fixed.stream_chat_completion(
                            model_id, messages, routing_options=routing_options, **params
                        )
                    ) as chunks:
                        async for chunk in chunks:
                            yield chunk
            except EngineDeclined as declined:
                raise self._declined_where_chosen(model_id, declined) from declined
            return
        plan = self._plan(model_id, messages, routing_options, params)
        ledger = AttemptLedger()

        def run(
            pool: Any,
            name: str,
            options: RoutingRequestOptions,
            outcome: StreamOutcome,
            step: _Step,
            *,
            may_decline: bool,
        ) -> Any:
            # Closed with this generator, so a client that leaves closes the
            # pool's stream at once, and with it the connection the admission
            # process reads a cancellation from.
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
                    left_unsent=_declined if may_decline else None,
                )
            )

        step = _Step()
        if plan.local is not None:
            outcome = StreamOutcome()
            # Pushed around the whole step, as the shared router pushes its own
            # dispatch terms around a stream: the adapter reads them as it sends.
            with req_ctx.push(**admission_context(plan.terms)):
                async with run(
                    local_pool, _LOCAL, plan.local, outcome, step, may_decline=True
                ) as chunks:
                    async for chunk in chunks:
                        yield chunk
            if outcome.served:
                return
        ended = bool(ledger.errors) and ledger.refusals_end_plan(model_id)
        cloud = None if ended else self._cloud_options(model_id, plan)
        if cloud is not None:
            step = self._cloud_step(ledger, plan)
            self._log_offload(model_id, plan, step, ledger)
            outcome = StreamOutcome()
            async with run(cloud_pool, _CLOUD, cloud, outcome, step, may_decline=False) as chunks:
                async for chunk in chunks:
                    yield chunk
            if outcome.served:
                return
        else:
            step = _Step(after_local=plan.local is not None, earlier=tuple(ledger.attempts))
        self._finish(ledger, model_id, routing_options, plan, step)

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
